"""One redaction check for every test that says a secret was withheld.

``assertNotIn(secret, text)`` passes when a single character of the secret is changed or when
half of it survives a cut. ``assert_redacted`` fails if any 4-character piece of the secret is
still in the text (so every longer piece and every whole token of it is covered too), and, when
a label is given, if that label was lost: the reader must still see what was withheld.
"""

MIN_PIECE = 4


def secret_pieces(secret: str, n: int = MIN_PIECE) -> set[str]:
    """Every n-character window of *secret*; the whole secret when it is shorter than n."""
    if len(secret) <= n:
        return {secret}
    return {secret[i : i + n] for i in range(len(secret) - n + 1)}


def assert_redacted(text: str, secret: str, label: str | None = None, msg: object = None) -> None:
    assert isinstance(text, str), type(text)
    assert secret, "an empty secret checks nothing"
    leaked = sorted(p for p in secret_pieces(secret) if p in text)
    where = f" ({msg})" if msg is not None else ""
    assert not leaked, f"secret pieces survived redaction{where}: {leaked[:5]} in {text[:200]!r}"
    if label is not None:
        assert label in text, f"label {label!r} was lost{where}: {text[:200]!r}"
