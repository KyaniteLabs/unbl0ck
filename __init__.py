"""UNBL0CK: one queue of everything blocked on you (Kanban cards, chats, live prompts, pairing requests).

register() registers a slash command, a CLI command, and stashes its context so the
dashboard backend can use the host-owned LLM for "Ask about this card". Everything that
touches Kanban or chat history is imported lazily when a command or route runs."""

# Set by register(ctx); read by dashboard/plugin_api.py through the loaded module.
_PLUGIN_CTX = None


def _needs():
    from . import needs  # noqa: PLC0415  (lazy on purpose)

    return needs


def _summary(limit: int = 12) -> str:
    q = _needs().build_queue()
    if not q["count"]:
        return "Nothing is blocked on you."
    lines = [f"{q['count']} thing(s) waiting on you:"]
    for it in q["items"][:limit]:
        where = (f"{it['board']}:{it['task_id']}" if it["source"] == "kanban"
                 else f"{it.get('kind') or 'prompt'} {it['profile']}" if it["source"] == "prompt"
                 else f"pairing {it.get('platform')}" if it["source"] == "pairing"
                 else f"chat {it.get('platform') or it['profile']}")
        lead = it.get("bluf") or " ".join(str(it.get("ask") or "").split())[:140]
        lines.append(f"- [{where}] {it['title']}: {lead}")
    if q["count"] > limit:
        lines.append(f"…and {q['count'] - limit} more. Open /unbl0ck in Desktop.")
    for err in q["errors"]:
        lines.append(f"(source error: {err})")
    return "\n".join(lines)


def slash_unbl0ck(raw_args: str = "") -> str:
    return _summary()


def _setup_cli(parser):
    parser.add_argument("--json", action="store_true", help="print the raw queue")
    parser.add_argument("--pack", action="store_true", help="also write the Pocket-format pack")


def _cli(args):
    import json

    n = _needs()
    if args.json or args.pack:
        q = n.build_queue()
        if args.pack:
            print(f"pack: {n.write_pocket_pack(q)}")
        if args.json:
            print(json.dumps(q, indent=1))
        return
    print(_summary())


def register(ctx):
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx
    ctx.register_command("unbl0ck", slash_unbl0ck, description="What is blocked on you right now")
    ctx.register_cli_command("unbl0ck", "List what is blocked on you (Kanban, chats, live prompts, pairing)", _setup_cli, _cli)
    # Live prompts: stock observers for every point an agent blocks on a human (clarify,
    # approval, admin password). Observers only; a failure here never touches the prompt.
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook:
        register_hook("on_human_input_request", lambda **kw: _needs().record_wait(**kw))
        register_hook("on_human_input_resolved", lambda **kw: _needs().clear_wait(**kw))
