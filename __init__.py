"""UNBL0CK: one queue of everything blocked on you, inside and outside Hermes.

register() registers a slash command, a CLI command, and stashes its context so the
dashboard backend can use the host-owned LLM for "Ask about this card". Everything that
touches Kanban or chat history is imported lazily when a command or route runs."""

# Set by register(ctx); read by dashboard/plugin_api.py through the loaded module.
_PLUGIN_CTX = None


def _needs():
    from . import needs  # noqa: PLC0415  (lazy on purpose)

    return needs


APPROVE_COMMAND_SHOWN = 400


def _summary(limit: int = 12) -> str:
    n = _needs()
    q = n.build_queue()
    c = lambda value, size=200: n._clean(value, size)  # noqa: E731  (display sanitizer)
    if not q["count"] and not q["errors"] and not q["items"]:
        return "Nothing is blocked on you."
    asks = [it for it in q["items"] if it.get("tier") not in ("stuck", "answered")]
    held = len(q["items"]) - len(asks)
    lines = [f"{q['count']} thing(s) waiting on you:" if q["count"]
             else "No asks detected; the queue is incomplete." if q["errors"]
             else "No asks, but listed cards are stuck or answered:"]
    for it in asks[:limit]:
        where = (f"{c(it['board'], 80)}:{c(it['task_id'], 80)}" if it["source"] == "kanban"
                 else f"{c(it.get('kind') or 'prompt', 40)} {c(it.get('profile'), 80)}" if it["source"] == "prompt"
                 else f"pairing {c(it.get('platform'), 40)}" if it["source"] == "pairing"
                 else f"outside {c(it.get('kind'), 40)} · {c(it.get('act_where'), 120)}" if it["source"] == "outside"
                 else f"chat {c(it.get('platform') or it.get('profile'), 80)}"
                 + (f" with {c(it['chat_with'], 120)}" if it.get("chat_with") else ""))
        lead = c(it.get("bluf") or it.get("ask"), 140)
        lines.append(f"- [{where}] {c(it.get('title'), 160)}: {lead}")
        if it.get("approve_command"):
            # A command cut short is a different command: show it whole or not at all. The
            # longest valid one (a 250-character profile name) is under 330 characters.
            cmd = c(it["approve_command"], APPROVE_COMMAND_SHOWN + 1)
            lines.append(
                f"  approve: {cmd}"
                if len(cmd) <= APPROVE_COMMAND_SHOWN
                else "  approve: (command too long to show here; use the Desktop card)"
            )
    if q["count"] > limit:
        lines.append(f"…and {q['count'] - limit} more. Open /unblock in Desktop.")
    if held:
        lines.append(f"{held} stuck or answered card(s) are also listed in Desktop.")
    for err in q["errors"]:
        lines.append(f"(source error: {c(err, 300)})")
    return "\n".join(lines)


# Surfaces only the machine's owner sits at. Anywhere else (Telegram, Discord, Slack, ...)
# any admitted sender can type /unbl0ck, so it gets a count, never the cross-profile queue
# with its approval commands and prompt text. An unknown or missing platform counts as remote.
LOCAL_SURFACES = frozenset({"cli", "tui", "desktop", "local"})


def _count_only() -> str:
    q = _needs().build_queue()
    held = any(it.get("tier") in ("stuck", "answered") for it in q["items"])
    if not q["count"] and not q["errors"] and not held:
        return "Nothing is blocked on you."
    lead = f"{q['count']} thing(s) waiting on you. " if q["count"] else ""
    if not q["count"] and held and not q["errors"]:
        lead = "No asks, but stuck or answered cards are listed. "
    if q["errors"]:
        lead += "The queue is incomplete. "
    return (lead + "Open UNBL0CK in Hermes Desktop or run "
            "hermes unbl0ck on the machine to see them.")


def slash_unbl0ck(raw_args: str = "", *, platform: str | None = None) -> str:
    """Stock Hermes passes ``platform`` to handlers that declare it."""
    if str(platform or "").lower() in LOCAL_SURFACES:
        return _summary()
    return _count_only()


def _setup_cli(parser):
    parser.add_argument("--json", action="store_true", help="print the raw queue")
    parser.add_argument("--pack", action="store_true", help="also write the Pocket-format pack")


def _cli(args):
    import json
    import sys

    n = _needs()
    if args.json or args.pack:
        q = n.build_queue()
        if args.pack:
            print(f"pack: {n.write_pocket_pack(q)}", file=sys.stderr if args.json else sys.stdout)
        if args.json:
            print(json.dumps(q, indent=1))
        return
    print(_summary())


FILE_SCHEMA = {
    "name": "unbl0ck_file",
    "description": "File one thing waiting on the human outside Hermes, or resolve a previously filed item. "
                   "This only records an agent claim; it never sends mail, opens references or acts in another app.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["file", "resolve"], "default": "file"},
            "kind": {"type": "string", "enum": ["draft", "notes", "approval", "other"]},
            "title": {"type": "string", "maxLength": 160},
            "body": {"type": "string", "maxLength": 8000},
            "act_where": {"type": "string", "maxLength": 120, "description": "Where the human acts, e.g. Outlook → Drafts or Plaud app"},
            "ref": {"type": "string", "maxLength": 300, "description": "Plain-text id, URL or path; never opened automatically"},
            "dedupe_key": {"type": "string", "maxLength": 120, "description": "Re-file this key to update in place; also usable with resolve"},
            "id": {"type": "string", "description": "Previously returned item id, for resolve"},
            "version": {"type": "string", "description": "The version returned when the item was filed. Required when resolving by id; a changed item is not resolved"},
        },
        "description": "file requires kind, title, body and act_where; resolve requires either id or dedupe_key",
        "additionalProperties": False,
    },
}


def unbl0ck_file(args: dict, **kwargs) -> str:
    import json

    try:
        return json.dumps(_needs().file_outside(args, **kwargs))
    except Exception as exc:  # tools return JSON errors, never break an agent turn
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def register(ctx):
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx
    ctx.register_command("unbl0ck", slash_unbl0ck, description="What is blocked on you right now")
    ctx.register_cli_command("unbl0ck", "List what is blocked on you (Kanban, outside Hermes, chats, live prompts, pairing)", _setup_cli, _cli)
    ctx.register_tool("unbl0ck_file", "unbl0ck", FILE_SCHEMA, unbl0ck_file, description=FILE_SCHEMA["description"])
    # Live prompts: stock observers for every point an agent blocks on a human (clarify,
    # approval, admin password). Observers only; a failure here never touches the prompt.
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook:
        register_hook("on_human_input_request", lambda **kw: _needs().record_wait(**kw))
        register_hook("on_human_input_resolved", lambda **kw: _needs().clear_wait(**kw))
