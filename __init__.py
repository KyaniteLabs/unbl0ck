"""UNBL0CK: one queue of everything blocked on you (Kanban cards + Bot Mode chats).

register() is pure: it only registers a slash command and a CLI command. Everything that
touches Kanban or chat history is imported lazily when a command runs."""


def _needs():
    from . import needs  # noqa: PLC0415  (lazy on purpose)

    return needs


def _summary(limit: int = 12) -> str:
    q = _needs().build_queue()
    if not q["count"]:
        return "Nothing is blocked on you."
    lines = [f"{q['count']} thing(s) waiting on you:"]
    for it in q["items"][:limit]:
        where = f"{it['board']}:{it['task_id']}" if it["source"] == "kanban" else f"chat {it['profile']}"
        lines.append(f"- [{where}] {it['title']}: {it['ask'][:140]}")
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
    ctx.register_command("unbl0ck", slash_unbl0ck, description="What is blocked on you right now")
    ctx.register_cli_command("unbl0ck", "List what is blocked on you (Kanban + Bot Mode)", _setup_cli, _cli)
