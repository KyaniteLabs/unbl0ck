"""Contract for a future provider adapter (issue #39). Nothing here reads a mailbox, calls a
network or is wired into the queue: it fixes the vocabulary and the reconciliation rules that
an adapter must obey, so they can be tested before any connector is approved.

The rules, in one place:

* An item is identified by ``(provider, account, external_id)``; duplicates are no-ops.
* ``seq`` orders a provider's revisions of one item. An older revision never overwrites a newer one.
* Completion has an evidence level. An agent's ``resolve`` is ``asserted``; only a read back from
  the provider is ``verified``. An asserted completion is never reported as verified, and a
  provider that still shows the item open contradicts it.
* A failed read (access revoked, rate limited) says nothing about the items: absence is never
  completion, nothing is deleted, and the cursor does not advance.
* Reading is separate from writing. ``capabilities`` lists what the adapter may do, ``write`` is
  never part of the default, and a write needs its own approval outside this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

Key = tuple[str, str, str]  # (provider, account id, external id)

READ = "read"
WRITE = "write"  # send, reply or edit in the provider; never granted by default

OPEN, DONE, DELETED = "open", "done", "deleted"
ASSERTED, VERIFIED = "asserted", "provider_verified"


@dataclass(frozen=True)
class ExternalItem:
    provider: str
    account: str
    external_id: str
    seq: int  # monotonic per item (etag counter, change number, modified time)
    revision: str  # opaque provider revision, used as the card version
    state: str = OPEN  # open, done or deleted as the provider reports it now
    kind: str = "other"  # draft, notes, approval, other

    @property
    def key(self) -> Key:
        return (self.provider, self.account, self.external_id)


@dataclass(frozen=True)
class ChangeBatch:
    """One incremental read. Only ``ok`` carries items and moves the cursor."""

    status: str = "ok"  # ok, access_revoked, rate_limited
    items: tuple[ExternalItem, ...] = ()
    cursor: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class Local:
    """What the queue already holds for an external item."""

    seq: int
    revision: str
    state: str = OPEN  # open, done, gone, held
    evidence: str | None = None  # None while open; ASSERTED or VERIFIED once done
    acted_revision: str | None = None  # the revision the human dismissed or answered
    pending_edit: bool = False  # the human has an unsent change in flight


@dataclass(frozen=True)
class Action:
    op: str
    key: Key | None = None
    detail: str = ""


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    cursor: str | None = None  # None means: keep the old cursor
    retry_after: float | None = None


class Adapter(Protocol):
    """What a provider adapter provides. All methods are read-only unless ``WRITE`` is listed."""

    name: str
    capabilities: frozenset[str]  # subset of {READ, WRITE}; the default is {READ}

    def identity(self) -> str:
        """An opaque account id: never an address, name or token."""

    def read_changes(self, cursor: str | None) -> ChangeBatch:
        """Changes since ``cursor``; must tolerate repeats, reorderings and a revoked grant."""


def reconcile(local: Mapping[Key, Local], batch: ChangeBatch, accounts: Iterable[tuple[str, str]] = ()) -> Plan:
    """Turn one batch into actions. Pure: no I/O, no clock. ``accounts`` are the (provider, account)
    pairs the batch was read for, needed to hold their items when access is revoked."""
    plan = Plan()
    if batch.status == "rate_limited":
        plan.retry_after = batch.retry_after
        return plan  # no state change, cursor stays: the same read is retried later
    if batch.status == "access_revoked":
        gone = set(accounts)
        plan.actions = [Action("hold", k, "access revoked; items may be stale") for k, v in local.items()
                        if (k[0], k[1]) in gone and v.state != "gone"]
        return plan  # absence after a revoked grant is not completion or deletion
    plan.cursor = batch.cursor
    seen: dict[Key, int] = {}
    for item in batch.items:
        cur = local.get(item.key)
        if seen.get(item.key, -1) >= item.seq:
            continue  # the same event twice in one batch
        seen[item.key] = item.seq
        plan.actions.extend(_one(item, cur))
    return plan


def _one(item: ExternalItem, cur: Local | None) -> list[Action]:
    k = item.key
    if item.state == DELETED:
        return [Action("remove", k, "deleted at the provider")] if cur and cur.state != "gone" else []
    if cur is None:
        return [] if item.state == DONE else [Action("add", k)]
    if item.seq < cur.seq:
        return [Action("ignore_stale", k)]
    if item.seq == cur.seq and item.revision == cur.revision and cur.state != "held":
        if item.state == DONE and cur.evidence == ASSERTED:
            return [Action("verify_done", k, "provider now confirms the agent's claim")]
        return []  # duplicate event
    if item.state == DONE:
        return [Action("complete_verified", k, "provider reports it done")]
    out: list[Action] = []
    if cur.state == DONE and cur.evidence == ASSERTED:
        out.append(Action("contradicted", k, "an agent said done; the provider still shows it open"))
    elif cur.state == DONE:
        out.append(Action("reopen", k, "reopened at the provider"))
    if item.revision != cur.revision:
        if cur.pending_edit:
            out.append(Action("conflict", k, "changed at the provider while a local edit is unsent; nothing was written"))
        out.append(Action("update", k, item.revision))
        if cur.acted_revision is not None and cur.acted_revision != item.revision:
            out.append(Action("resurface", k, "changed since you dealt with it"))
    elif cur.state == "held":
        out.append(Action("release_hold", k))
    return out


def completion_label(evidence: str | None) -> str:
    """The only wording a surface may use for a finished item."""
    return {VERIFIED: "Done (confirmed at the source)", ASSERTED: "Reported done by an agent (not confirmed)"}.get(
        evidence or "", "Open")


def may_write(adapter: Any) -> bool:
    return WRITE in getattr(adapter, "capabilities", frozenset())
