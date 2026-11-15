"""Contract for a future provider adapter (issue #39). Nothing here reads a mailbox, calls a
network or is wired into the queue: it fixes the vocabulary and the reconciliation rules that
an adapter must obey, so they can be tested before any connector is approved.

The rules, in one place:

* An item is identified by ``(provider, account, external_id)``; duplicates are no-ops. An
  external id must identify the item uniquely inside its account (``imap_external_id`` shows how
  for a mailbox message).
* ``seq`` (a non-negative integer) orders a provider's revisions of one item. An older revision
  never overwrites a newer one, and every action that moves an item carries its ``seq`` so a
  store can keep the watermark, including for items it holds no card for (a tombstone).
* Completion has an evidence level. An agent's ``resolve`` is ``asserted``; a provider signal
  that is only a guess is ``heuristic``; only a read back that the provider itself states is
  ``verified``. A weaker level is never reported as a stronger one, and a provider that still
  shows the item open contradicts an asserted or heuristic completion.
* A failed read (access revoked, authentication failed, rate limited, any other error) says
  nothing about the items: absence is never completion, nothing is deleted, and the cursor does
  not advance. Only ``ok`` carries items and moves the cursor.
* A read that stops early says so (``complete``, ``omitted``, ``stop_reason``): a partial read is
  never the same result as a complete one.
* Every read is bound to the account scope it was made for; items outside that scope are rejected.
* Reading is separate from writing. ``capabilities`` lists what the adapter may do, ``write`` is
  never part of the default, and a write needs its own approval outside this contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Protocol

Key = tuple[str, str, str]  # (provider, account id, external id)

READ = "read"
WRITE = "write"  # send, reply or edit in the provider; never granted by default

OPEN, DONE, DELETED = "open", "done", "deleted"
LEFT_VIEW = "left_view"  # the provider stopped listing it (moved, filtered); existence not confirmed
ITEM_STATES = frozenset({OPEN, DONE, DELETED, LEFT_VIEW})
ASSERTED, HEURISTIC, VERIFIED = "asserted", "heuristic", "provider_verified"
_RANK = {ASSERTED: 1, HEURISTIC: 2, VERIFIED: 3}
CONFIRMED = "confirmed"  # the provider itself says it is done
GUESSED = "heuristic"  # inferred (for example a draft that vanished and a message in Sent)

OK, ACCESS_REVOKED, AUTH_FAILED, RATE_LIMITED = "ok", "access_revoked", "auth_failed", "rate_limited"
HOLD_STATUSES = frozenset({ACCESS_REVOKED, AUTH_FAILED})
ACCESS = "access"  # hold reason: a read of the account failed (LEFT_VIEW is the other reason)
STATUSES = frozenset({OK, RATE_LIMITED}) | HOLD_STATUSES


@dataclass(frozen=True)
class ExternalItem:
    provider: str
    account: str
    external_id: str
    seq: int  # non-negative, monotonic per item (etag counter, change number, modified time)
    revision: str  # opaque provider revision, used as the card version
    state: str = OPEN  # open, done, deleted or left_view as the provider reports it now
    kind: str = "other"  # draft, notes, approval, other
    confidence: str = CONFIRMED  # how sure a ``done`` is: CONFIRMED, or GUESSED for a heuristic

    @property
    def key(self) -> Key:
        return (self.provider, self.account, self.external_id)


@dataclass(frozen=True)
class ChangeBatch:
    """One incremental read. Only ``ok`` carries items and moves the cursor."""

    status: str = OK  # ok, access_revoked, auth_failed, rate_limited; anything else is an error
    items: tuple[ExternalItem, ...] = ()
    cursor: str | None = None
    retry_after: float | None = None
    accounts: tuple[tuple[str, str], ...] = ()  # the (provider, account) pairs this read was made for
    complete: bool = True  # False when a bounded read stopped before the end
    omitted: int | None = None  # items left unread when incomplete; None when the adapter cannot count
    stop_reason: str | None = None  # why it stopped: "item_limit", "time_limit", ...


@dataclass(frozen=True)
class Local:
    """What the queue already holds for an external item."""

    seq: int
    revision: str
    state: str = OPEN  # open, done, gone (a tombstone), held (legacy spelling of ``held=True``)
    evidence: str | None = None  # None while open; ASSERTED, HEURISTIC or VERIFIED once done
    acted_revision: str | None = None  # the revision the human dismissed or answered
    pending_edit: bool = False  # the human has an unsent change in flight
    held: bool | str = False  # ACCESS or LEFT_VIEW (True: legacy ACCESS); keeps the open or done state underneath


@dataclass(frozen=True)
class Action:
    op: str
    key: Key | None = None
    detail: str = ""
    seq: int | None = None  # the watermark this action moves the item to
    revision: str | None = None
    evidence: str | None = None  # for completion actions
    hold: str | None = None  # for ``hold``: ACCESS or LEFT_VIEW


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    cursor: str | None = None  # None means: keep the old cursor
    retry_after: float | None = None
    complete: bool = True  # False: the read stopped early or items were rejected; not a full result
    omitted: int | None = 0  # items not covered; None when unknown
    stop_reason: str | None = None
    error: str | None = None  # set when the read failed; nothing was applied


class Adapter(Protocol):
    """What a provider adapter provides. All methods are read-only unless ``WRITE`` is listed."""

    name: str
    capabilities: frozenset[str]  # subset of {READ, WRITE}; the default is {READ}

    def identity(self) -> str:
        """An opaque account id: never an address, name or token."""

    def read_changes(self, cursor: str | None) -> ChangeBatch:
        """Changes since ``cursor``; must tolerate repeats, reorderings and a revoked grant."""


def imap_external_id(mailbox: str, uidvalidity: int, uid: int) -> str:
    """The external id of one IMAP message. A UID is only unique inside one mailbox and one
    UIDVALIDITY epoch, so the id carries both; the mailbox is hashed so no folder name is stored."""
    for name, value in (("uidvalidity", uidvalidity), ("uid", uid)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(mailbox, str) or not mailbox:
        raise ValueError("mailbox is required")
    return f"{hashlib.sha256(mailbox.encode('utf-8')).hexdigest()[:16]}:{uidvalidity}:{uid}"


def _valid_seq(seq: Any) -> bool:
    return isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0


def reconcile(local: Mapping[Key, Local], batch: ChangeBatch, accounts: Iterable[tuple[str, str]] = ()) -> Plan:
    """Turn one batch into actions. Pure: no I/O, no clock. The account scope (``accounts`` or
    ``batch.accounts``) is required for every read except a rate limit: a failed batch carries no
    items, so the scope is the only way to know whose items to hold. The events of an ``ok`` batch
    are applied one by one in the order received, so a batch ends exactly where the same events
    delivered one batch each would end."""
    plan = Plan()
    if batch.status == RATE_LIMITED:
        plan.retry_after = batch.retry_after
        plan.complete = False
        plan.omitted = None
        plan.stop_reason = RATE_LIMITED
        return plan  # no state change, cursor stays: the same read is retried later
    scope = _scope(accounts, batch)
    if batch.status in HOLD_STATUSES:
        plan.error = batch.status
        plan.complete = False
        plan.omitted = None
        plan.stop_reason = batch.status
        plan.actions = [Action("hold", k, "access failed; items may be stale", hold=ACCESS) for k, v in local.items()
                        if (k[0], k[1]) in scope and v.state != "gone" and not _hold(v)]
        return plan  # absence after a failed grant is not completion or deletion
    if batch.status != OK:
        plan.error = f"unrecognised batch status {batch.status!r}; nothing was applied"
        plan.complete = False
        plan.omitted = None
        plan.stop_reason = "error"
        return plan  # an unknown status is a failure, never a success
    state = dict(local)
    rejected = 0
    for item in batch.items:
        if (item.provider, item.account) not in scope:
            plan.actions.append(Action("reject_item", item.key, "outside the account scope of this read"))
            rejected += 1
            continue
        if not _valid_seq(item.seq):
            plan.actions.append(Action("reject_item", item.key, "seq must be a non-negative integer"))
            rejected += 1
            continue
        cur = state.get(item.key)
        acts = _one(item, cur)
        plan.actions.extend(acts)
        state.update(apply({} if cur is None else {item.key: cur}, Plan(acts)))
    plan.complete = batch.complete and not rejected
    plan.actions.extend(Action("release_hold", k, "reads work again") for k, v in state.items()
                        if (k[0], k[1]) in scope and _releases(v, None, plan.complete))
    if not plan.complete:
        plan.omitted = None if batch.omitted is None and not batch.complete else (batch.omitted or 0) + rejected
        reasons = [r for r in (batch.stop_reason or (None if batch.complete else "unspecified"),
                               "rejected_items" if rejected else None) if r]
        plan.stop_reason = "; ".join(reasons)
    plan.cursor = batch.cursor if not rejected else None  # a batch with rejected items is not accepted
    return plan


def _scope(accounts: Iterable[tuple[str, str]], batch: ChangeBatch) -> set[tuple[str, str]]:
    given, declared = set(accounts), set(batch.accounts)
    if given and declared and given != declared:
        raise ValueError("the accounts argument and the batch scope disagree")
    scope = given or declared
    if not scope:
        raise ValueError("an account scope is required: pass accounts or set ChangeBatch.accounts")
    return scope


def _hold(cur: Local) -> str | None:
    """Why ``cur`` is held (ACCESS or LEFT_VIEW), or None. ``held=True`` and the state "held" are
    the legacy spellings of an access hold."""
    if cur.state == "gone":
        return None
    if cur.held in (ACCESS, LEFT_VIEW):
        return cur.held
    return ACCESS if cur.held is True or cur.state == "held" else None


def _releases(cur: Local, item: ExternalItem | None, complete: bool) -> bool:
    """The one release rule. An access hold ends when a successful read of its account mentions
    the item (even as a stale event) or, unmentioned, when the read is complete. A left-view hold
    ends only when the provider lists the item again with a newer seq; reads working says nothing
    about where it went."""
    if item is not None and item.state not in ITEM_STATES - {LEFT_VIEW}:
        return False
    hold = _hold(cur)
    if hold == LEFT_VIEW:
        return item is not None and item.seq > cur.seq
    return hold == ACCESS and (item is not None or complete)


def _view(cur: Local) -> tuple[str, str | None]:
    return (DONE if cur.evidence else OPEN) if cur.state == "held" else cur.state, cur.evidence


def _same_seq_conflict(item: ExternalItem, cur: Local) -> bool:
    """An event at the stored seq must repeat what the provider said at that seq: same revision
    and the same state. An agent's claim or a guessed completion is not a provider state, so the
    provider showing that item open or done at the same seq contradicts or upgrades it instead."""
    if item.seq != cur.seq:
        return False
    if item.revision != cur.revision:
        return True
    if cur.state == "gone":
        return item.state not in (DELETED, LEFT_VIEW)
    if _hold(cur) == LEFT_VIEW:
        return item.state != LEFT_VIEW
    state, ev = _view(cur)
    if item.state in (DELETED, LEFT_VIEW) or state == OPEN:
        return item.state != state
    return ev == VERIFIED and item.state != DONE


def _one(item: ExternalItem, cur: Local | None) -> list[Action]:
    k, s, r = item.key, item.seq, item.revision
    known, guessed = item.state in ITEM_STATES, item.confidence != CONFIRMED
    ev = HEURISTIC if guessed else VERIFIED
    if cur is None:
        if not known or item.state == LEFT_VIEW:  # no card, but a later older event must stay stale
            return [Action("watermark", k, "no card: only the sequence is kept", seq=s, revision=r if known else None)]
        if item.state == DELETED:
            return [Action("tombstone", k, "deleted at the provider; the sequence is kept", seq=s, revision=r)]
        if item.state == DONE:
            return [Action("track_done", k, "already done at the provider; no card", seq=s, revision=r, evidence=ev)]
        return [Action("add", k, seq=s, revision=r)]
    out: list[Action] = [Action("release_hold", k)] if _releases(cur, item, False) else []
    if s < cur.seq:
        return out + [Action("ignore_stale", k)]
    if not known:  # neither open work nor a deletion: report it and change nothing but the watermark
        return [Action("unknown_state", k, "the provider reported a state this contract does not know", seq=s)]
    if _same_seq_conflict(item, cur):
        return out + [Action("seq_collision", k, "same seq, different content; the stored one was kept")]
    newer = [Action("advance", k, seq=s, revision=r)] if s > cur.seq else []  # same content, newer seq
    state, cur_ev = _view(cur)
    edit = cur.pending_edit
    conflict = Action("conflict", k, "changed at the provider while a local edit is unsent; nothing was written")
    if cur.state == "gone":
        if item.state in (DELETED, LEFT_VIEW):
            return newer
        out.append(Action("restore", k, "back at the provider", seq=s, revision=r))
        if item.state == DONE:
            out.append(Action("complete_unconfirmed" if guessed else "complete_verified", k, seq=s, revision=r, evidence=ev))
        return out
    if item.state == DELETED:
        if edit:
            out.append(conflict)
        return out + [Action("remove", k, "deleted at the provider", seq=s, revision=r)]
    if item.state == LEFT_VIEW:
        if _hold(cur) == LEFT_VIEW:
            return newer
        return [Action("hold", k, "no longer listed where it was read; existence is not confirmed", seq=s, revision=r,
                       hold=LEFT_VIEW)]
    if item.state == DONE:
        if state == DONE and _RANK.get(cur_ev, 0) >= _RANK[ev]:
            if r != cur.revision:
                return out + [Action("update", k, r, seq=s, revision=r)]
            return out + newer
        if edit:
            out.append(conflict)
        if state == DONE and cur_ev == ASSERTED and r == cur.revision and not guessed:
            return out + [Action("verify_done", k, "provider now confirms the agent's claim", seq=s, revision=r, evidence=VERIFIED)]
        if guessed:
            return out + [Action("complete_unconfirmed", k, "inferred, not confirmed at the source", seq=s, revision=r, evidence=HEURISTIC)]
        return out + [Action("complete_verified", k, "provider reports it done", seq=s, revision=r, evidence=VERIFIED)]
    # The provider shows it open.
    if state == DONE:
        if cur_ev in (ASSERTED, HEURISTIC):
            out.append(Action("contradicted", k, "an agent said done; the provider still shows it open", seq=s, revision=r))
        else:
            out.append(Action("reopen", k, "reopened at the provider", seq=s, revision=r))
    if r != cur.revision:
        if edit:
            out.append(conflict)
        out.append(Action("update", k, r, seq=s, revision=r))
        if cur.acted_revision is not None and cur.acted_revision != r:
            out.append(Action("resurface", k, "changed since you dealt with it"))
    return out if any(a.seq is not None for a in out) else out + newer


def _advance(cur: Local, a: Action) -> Local:
    """Move the watermark: seq and revision always together, for every action that carries a newer seq."""
    if a.seq is None or a.seq <= cur.seq:
        return cur
    return replace(cur, seq=a.seq, revision=cur.revision if a.revision is None else a.revision)


_NEW = {"add": OPEN, "tombstone": "gone", "watermark": "gone", "track_done": DONE}  # actions that create a stored row


def apply(local: Mapping[Key, Local], plan: Plan) -> dict[Key, Local]:
    """Reference model of what a store does with a plan. Pure; tests use it to replay batches."""
    out = dict(local)
    for a in plan.actions:
        cur = out.get(a.key) if a.key else None
        if a.op in _NEW and cur is None:
            out[a.key] = Local(a.seq, a.revision or "", _NEW[a.op], a.evidence if a.op == "track_done" else None)
            continue
        if cur is None:
            continue
        if a.op == "hold":
            cur = replace(cur, held=a.hold or ACCESS)
        elif a.op == "release_hold":
            cur = replace(cur, held=False, state=(DONE if cur.evidence else OPEN) if cur.state == "held" else cur.state)
        elif a.op in ("complete_verified", "complete_unconfirmed", "verify_done"):
            cur = replace(cur, state=DONE, evidence=a.evidence)
        elif a.op in ("contradicted", "reopen", "restore"):
            cur = replace(cur, state=OPEN, evidence=None)
        elif a.op == "remove":
            cur = replace(cur, state="gone", held=False, evidence=None, pending_edit=False)
        elif a.op == "resurface":
            cur = replace(cur, acted_revision=None)
        out[a.key] = _advance(cur, a)
    return out


def completion_label(evidence: str | None) -> str:
    """The only wording a surface may use for a finished item."""
    return {VERIFIED: "Done (confirmed at the source)",
            HEURISTIC: "Probably done (inferred, not confirmed at the source)",
            ASSERTED: "Reported done by an agent (not confirmed)"}.get(evidence or "", "Open")


def may_write(adapter: Any) -> bool:
    return WRITE in getattr(adapter, "capabilities", frozenset())
