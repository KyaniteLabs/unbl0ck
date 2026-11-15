"""Labeled corpus for ask detection (issue #40).

Each case is (id, detector, text, kind, expected, tags). ``detector`` is "chat" (the last bot turn
of a chat) or "kanban" (a block reason, with the card kind in ``kind``). ``expected`` is whether
the item belongs in the queue as an ask. Tags name the dimension a case covers so the report can
show coverage: platform (cli, discord, telegram, desktop), language (en, es, fr, de, ja),
name form (named, role, none), shape (direct, indirect, group, packet, report, relayed, historical,
self_answered, signoff, stuck).

KNOWN_GAPS lists the ids the heuristic is known to get wrong today. They are real limitations
(not regressions): the test pins the exact set, so fixing one forces its removal and a new
failure forces a decision. Cases already fixed in earlier releases are not in KNOWN_GAPS."""

# The principal name the corpus is judged with: "named" cases contain it literally, so a detector
# that stopped matching the configured name could not pass on "your" alone.
PRINCIPAL = "Ada"

CASES = [
    # --- direct asks, English, several platforms -------------------------------------------
    ("en-direct-should-i", "chat", "Should I open the PR now?", None, True, ("cli", "en", "none", "direct")),
    ("en-direct-shall-i", "chat", "Built both variants. Shall I ship A?", None, True, ("desktop", "en", "none", "direct")),
    ("en-direct-want", "chat", "Do you want me to rebase first?", None, True, ("discord", "en", "none", "direct")),
    ("en-direct-need-you", "chat", "I need your approval before I touch prod. OK?", None, True, ("telegram", "en", "none", "direct")),
    ("en-direct-which", "chat", "Which option do you prefer, A or B?", None, True, ("cli", "en", "none", "direct")),
    ("en-direct-decision", "chat", "Decision needed: rotate the key now or Friday?", None, True, ("desktop", "en", "none", "direct")),
    ("en-direct-waiting", "chat", "Waiting on you: confirm the budget?", None, True, ("discord", "en", "none", "direct")),
    ("en-packet", "chat", "Decision packet for you: ship A or park B.", None, True, ("desktop", "en", "none", "packet")),
    ("en-packet-options", "chat", "Options for you:\n1. ship\n2. hold\n3. cancel", None, True, ("telegram", "en", "none", "packet")),
    ("en-packet-long", "chat", "x " * 600 + "Questions for you: which region first", None, True, ("cli", "en", "none", "packet")),
    # --- not asks: reports, sign-offs, answered questions, links ---------------------------
    ("en-report", "chat", "Deployed v6. All 914 tests pass.", None, False, ("cli", "en", "none", "report")),
    ("en-signoff", "chat", "Done. The file is saved. Anything else you need?", None, False, ("desktop", "en", "none", "signoff")),
    ("en-signoff-let-me-know", "chat", "All set. Let me know if you need anything.", None, False, ("telegram", "en", "none", "signoff")),
    ("en-signoff-hope", "chat", "Merged and deployed. Hope this helps!", None, False, ("discord", "en", "none", "signoff")),
    ("en-url-query", "chat", "Here is your link: https://example.com/search?q=1", None, False, ("cli", "en", "none", "report")),
    ("en-self-answered", "chat", "Why did the build fail? Because your lockfile was stale. Fixed.", None, False, ("desktop", "en", "none", "self_answered")),
    ("en-historical-packet", "chat", "Summary: decision packet archived last week. Nothing pending.", None, False, ("cli", "en", "none", "historical")),
    ("en-historical-closed", "chat", "The decision packet was resolved on Monday and the work shipped.", None, False, ("desktop", "en", "none", "historical")),
    ("en-compared", "chat", "I compared which option was faster and went with B. All tests pass.", None, False, ("cli", "en", "none", "report")),
    ("en-gate-idiom-in-report", "chat", "Tonight it worked at machine speed. The only open item is that's an allowlist change - Platform's, on your word.", None, False, ("desktop", "en", "none", "report")),
    ("en-rhetorical-no-you", "chat", "Is the build green? Yes, all checks passed.", None, False, ("cli", "en", "none", "self_answered")),
    ("en-relayed-instruction", "chat", "The reviewer said: should I merge this? I did not, and the branch is untouched.", None, False, ("discord", "en", "none", "relayed")),
    # --- other languages: the shipped patterns are English only ---------------------------
    ("es-direct", "chat", "¿Quieres que lo despliegue ahora o espero tu aprobación?", None, True, ("telegram", "es", "none", "direct")),
    ("fr-direct", "chat", "Veux-tu que je publie la version maintenant ?", None, True, ("discord", "fr", "none", "direct")),
    ("de-direct", "chat", "Soll ich den Pull Request jetzt öffnen?", None, True, ("cli", "de", "none", "direct")),
    ("ja-direct", "chat", "今すぐデプロイしてもよろしいですか？あなたの承認が必要です。", None, True, ("desktop", "ja", "none", "direct")),
    ("es-report", "chat", "Listo. Todas las pruebas pasaron.", None, False, ("telegram", "es", "none", "report")),
    ("de-report", "chat", "Fertig. Alle Tests bestanden.", None, False, ("cli", "de", "none", "report")),
    # --- indirect and group asks -----------------------------------------------------------
    ("en-indirect-hint", "chat", "I can go either way here. Someone with prod access should pick one.", None, True, ("discord", "en", "none", "indirect")),
    ("en-indirect-passive", "chat", "A decision is needed on the retention period before I continue.", None, True, ("telegram", "en", "none", "indirect")),
    ("en-group-unnamed", "chat", "Team, which region should we deploy to first?", None, True, ("discord", "en", "none", "group")),
    ("en-group-everyone", "chat", "Everyone: reply with your vote, A or B.", None, True, ("telegram", "en", "none", "group")),
    # --- kanban block reasons -------------------------------------------------------------
    ("kb-needs-input-name", "kanban", "Needs your decision on the vendor", "needs_input", True, ("kanban", "en", "named", "direct")),
    ("kb-needs-input-role", "kanban", "Operator input is needed", "needs_input", True, ("kanban", "en", "role", "direct")),
    ("kb-needs-input-gate", "kanban", "Human gate: approve the migration", "needs_input", True, ("kanban", "en", "role", "direct")),
    ("kb-credentials", "kanban", "API key expired, credentials needed", "needs_input", True, ("kanban", "en", "none", "direct")),
    ("kb-stuck-technical", "kanban", "Tests fail on the arm64 runner", None, False, ("kanban", "en", "none", "stuck")),
    ("kb-order-not-ask", "kanban", "Operator 14:02 stop all deploys", None, False, ("kanban", "en", "role", "relayed")),
    ("kb-es", "kanban", "Necesita tu decisión sobre el proveedor", "needs_input", True, ("kanban", "es", "none", "direct")),
    # --- the principal named literally (the configured principal_name) ------------------------
    ("kb-named-gate", "kanban", "Ada must approve the migration", None, True, ("kanban", "en", "named", "direct")),
    ("kb-named-waiting", "kanban", "Waiting on Ada for the budget", None, True, ("kanban", "en", "named", "direct")),
    ("kb-named-possessive", "kanban", "Ada's approval is required before launch", None, True, ("kanban", "en", "named", "direct")),
    ("kb-named-input", "kanban", "Ada has the vendor choice", "needs_input", True, ("kanban", "en", "named", "direct")),
    ("kb-named-lowercase", "kanban", "needs ada to pick the region", "needs_input", True, ("kanban", "en", "named", "direct")),
    ("kb-named-order", "kanban", "Ada 14:02 stop all deploys", None, False, ("kanban", "en", "named", "relayed")),
    ("kb-named-report", "kanban", "Tests fail on the arm64 runner; Ada was told", None, False, ("kanban", "en", "named", "stuck")),
    ("kb-other-name", "kanban", "Bob must approve the migration", None, False, ("kanban", "en", "named", "relayed")),
    ("kb-named-substring", "kanban", "Adage cache expired", "needs_input", False, ("kanban", "en", "named", "stuck")),
]

# Cases the current heuristic gets wrong. Not regressions; see the module docstring.
KNOWN_GAPS = {
    "en-relayed-instruction",  # false positive: a quoted question the bot declined is read as an ask
    "es-direct", "fr-direct", "de-direct", "ja-direct",
    "en-indirect-hint", "en-indirect-passive", "en-group-unnamed", "en-group-everyone",
    "kb-es",
}
