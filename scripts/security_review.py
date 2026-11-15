#!/usr/bin/env python3
"""Deliver commit-bound review requests to Hermes' native signed webhook ingress.

Only trusted default-branch code runs this relay. PR text, branches, patches and artifacts
are never executed or interpolated into its requests. Admission is not a review verdict.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

REPOSITORY = "KyaniteLabs/unbl0ck"
REPOSITORY_ID = 1451008069
ROUTE = "github-pr-security"
EVENT = "security_review_requested"
MAX_RESPONSE = 2 * 1024 * 1024


class BridgeError(RuntimeError):
    """Delivery or authenticated review reconciliation failed."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward credentials or signed requests to another URL


class Transport:
    def __init__(self, opener=None, sleep=time.sleep):
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep

    def request(self, url, *, token=None, body=None, headers=None):
        request_headers = {"Accept": "application/json", "User-Agent": "unbl0ck-security-review-relay"}
        request_headers.update(headers or {})
        if token:
            request_headers.update({"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2026-03-10"})
        for attempt in range(4):
            delay = 2 ** attempt
            try:
                request = urllib.request.Request(url, data=body, headers=request_headers)
                with self.opener.open(request, timeout=10) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise BridgeError("Response exceeded the relay's size limit")
                    try:
                        return response.status, json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise BridgeError("Receiver returned invalid JSON") from None
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status == 429:
                    # Native Hermes counts duplicate requests against its minute window.
                    # Short retries can permanently starve later PRs on every scan.
                    value = exc.headers.get("Retry-After", "")
                    try:
                        delay = float(value)
                    except ValueError:
                        try:
                            delay = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            delay = 61
                    if not math.isfinite(delay) or delay > 120:
                        exc.close()
                        raise BridgeError("Receiver requested a longer retry; reconcile again later") from None
                    delay = max(1, delay)
                exc.close()
                if status != 429 and not 500 <= status <= 599:
                    raise BridgeError(f"Request refused (HTTP {status})") from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                status = "transport unavailable"
            if attempt < 3:
                self.sleep(delay)
        raise BridgeError(f"Request failed after four attempts ({status})")


@dataclass(frozen=True)
class Snapshot:
    repository: str
    number: int
    head_sha: str
    base_sha: str
    url: str

    @classmethod
    def from_pr(cls, pr):
        try:
            number, base = pr["number"], pr["base"]
            repo = base["repo"]
            if type(number) is not int or number <= 0 or repo["id"] != REPOSITORY_ID or repo["full_name"].lower() != REPOSITORY.lower():
                raise ValueError()
            head_sha, base_sha = pr["head"]["sha"], base["sha"]
            if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head_sha, base_sha)):
                raise ValueError()
        except (KeyError, TypeError, AttributeError, ValueError):
            raise BridgeError("GitHub returned an invalid PR identity or commit snapshot") from None
        return cls(REPOSITORY, number, head_sha, base_sha, f"https://github.com/{REPOSITORY}/pull/{number}")

    def payload(self):
        # No timestamps/actions: every event for the same integration delta is identical,
        # including redelivery, scheduled reconciliation and manual backfill.
        identity = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        review_id = hashlib.sha256(identity).hexdigest()
        return review_id, json.dumps({**asdict(self), "review_id": review_id}, sort_keys=True, separators=(",", ":")).encode()


class Relay:
    def __init__(self, *, token, endpoint, secret, security_user_id, transport=None):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != f"/p/security/webhooks/{ROUTE}"):
            raise BridgeError("Configure an HTTPS webhook URL bound to the Security profile and github-pr-security route")
        if not token or len(secret.encode()) < 32:
            raise BridgeError("GitHub token and a webhook secret of at least 32 bytes are required")
        if type(security_user_id) is not int or security_user_id <= 0:
            raise BridgeError("Configure the verified Security GitHub account's numeric ID")
        self.token, self.endpoint, self.secret = token, endpoint, secret
        self.security_user_id = security_user_id
        self.transport = transport or Transport()

    def github(self, path):
        status, result = self.transport.request(f"https://api.github.com/repos/{REPOSITORY}/{path}", token=self.token)
        if status != 200:
            raise BridgeError("Unexpected GitHub response status")
        return result

    def open_numbers(self):
        for page in range(1, 11):
            prs = self.github(f"pulls?state=open&per_page=100&page={page}")
            if not isinstance(prs, list):
                raise BridgeError("GitHub returned an invalid PR list")
            for pr in prs:
                number = pr.get("number") if isinstance(pr, dict) else None
                if type(number) is not int or number <= 0:
                    raise BridgeError("GitHub returned an invalid PR number")
                yield number
            if len(prs) < 100:
                return
        raise BridgeError("Open PR pagination limit reached; reconcile remaining requests explicitly")

    def verdict(self, snapshot, review_id):
        reviews = []
        for page in range(1, 11):
            batch = self.github(f"pulls/{snapshot.number}/reviews?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise BridgeError("GitHub returned an invalid review list")
            reviews.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise BridgeError("Review pagination limit reached; inspect the PR explicitly")
        candidates = [r for r in reviews if isinstance(r, dict)
                      and isinstance(r.get("user"), dict)
                      and r["user"].get("id") == self.security_user_id
                      and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")]
        if not candidates:
            return None
        # An unbound newer decision must never leave an older approval green.
        if any(type(r.get("id")) is not int for r in candidates):
            raise BridgeError("GitHub returned an invalid Security review identity")
        review = max(candidates, key=lambda r: r["id"])
        markers = re.findall(r"<!-- unbl0ck-security-review (\{[^\n]*\}) -->", review.get("body") or "")
        if len(markers) != 1 or review.get("commit_id") != snapshot.head_sha:
            return None
        try:
            receipt = json.loads(markers[0])
        except ValueError:
            return None
        expected = {"review_id": review_id, "head_sha": snapshot.head_sha,
                    "base_sha": snapshot.base_sha, "verdict": "pass" if review["state"] == "APPROVED" else "fail"}
        if receipt != expected:
            return None
        # Detect a push or stacked-base advance during pagination/reconciliation.
        current = self.github(f"pulls/{snapshot.number}")
        if Snapshot.from_pr(current) != snapshot or current.get("state") != "open":
            raise BridgeError("PR changed during review reconciliation; request a fresh review")
        return {"number": snapshot.number, **expected, "status": expected["verdict"], "github_review_id": review["id"]}

    def deliver(self, number, *, include_drafts=False):
        if type(number) is not int or number <= 0:
            raise BridgeError("PR number must be a positive integer")
        pr = self.github(f"pulls/{number}")  # re-read live; never trust workflow event snapshots
        snapshot = Snapshot.from_pr(pr)
        if snapshot.number != number:
            raise BridgeError("GitHub returned a different PR number")
        if pr.get("state") not in ("open", "closed") or type(pr.get("draft")) is not bool:
            raise BridgeError("GitHub returned an invalid PR state")
        if pr["state"] != "open" or (pr["draft"] and not include_drafts):
            return {"number": number, "status": "skipped"}
        review_id, body = snapshot.payload()
        verdict = self.verdict(snapshot, review_id)
        if verdict:
            return verdict
        signature = "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        status, receipt = self.transport.request(self.endpoint, body=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": signature,
            "X-GitHub-Event": EVENT, "X-GitHub-Delivery": review_id,
        })
        if not isinstance(receipt, dict) or receipt.get("delivery_id") != review_id:
            raise BridgeError("Hermes did not return a matching delivery acknowledgement")
        accepted = status == 202 and receipt.get("status") == "accepted" and receipt.get("route") == ROUTE and receipt.get("event") == EVENT and "cron_job" not in receipt
        duplicate = status == 200 and receipt.get("status") == "duplicate"
        if not (accepted or duplicate):
            raise BridgeError("Hermes did not acknowledge the review route; check its configuration")
        return {"number": number, "head_sha": snapshot.head_sha, "base_sha": snapshot.base_sha,
                "review_id": review_id, "status": "pending", "delivery": receipt["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--include-drafts", action="store_true")
    args = parser.parse_args()
    try:
        relay = Relay(token=os.environ.get("GITHUB_TOKEN", ""), endpoint=os.environ.get("HERMES_SECURITY_WEBHOOK_URL", ""), secret=os.environ.get("HERMES_SECURITY_WEBHOOK_SECRET", ""), security_user_id=int(os.environ.get("HERMES_SECURITY_GITHUB_USER_ID", "0")))
        number = args.pr if args.pr is not None else (int(os.environ["PR_NUMBER"]) if os.environ.get("PR_NUMBER") else None)
        include_drafts = args.include_drafts or os.environ.get("INCLUDE_DRAFTS", "").lower() == "true"
        # Empty workflow_run PR metadata is common for forks; list live PRs in that case.
        numbers = [number] if number is not None else list(relay.open_numbers())
        failed = False
        for number in numbers:
            try:
                result = relay.deliver(number, include_drafts=include_drafts)
                print(json.dumps(result))
                if result["status"] in ("pending", "fail"):
                    print(f"PR {number}: Security review {result['status']}; no merge approval", file=sys.stderr)
                    failed = True
            except BridgeError as exc:
                print(f"Security review delivery for PR {number} failed: {exc}", file=sys.stderr)
                failed = True
        return int(failed)
    except (BridgeError, ValueError):
        # Environment, endpoint and transport diagnostics may contain secrets/private URLs.
        print("Security review relay could not initialize; check its configuration and GitHub access", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
