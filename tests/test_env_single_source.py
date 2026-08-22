"""The environment has ONE reader, and no read may carry a permissive literal default.

The standing gate for the absence-read-as-consent class, shared with ``review-kit`` where the
same fail-opens are refused. This kit has no deployment profile to re-derive, so the guard is on
the shape the class actually takes here: the fail-open is spelled
``os.environ.get(name, <literal>)``, the two-state read that cannot tell "absent" from "set to
nothing" and answers both with the literal. One resolver, ``client._resolve_secret``, owns that
decision in three states; a second reader, or a two-state read reappearing inside it, brings the
whole class back.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "consent_preference_kit"
_RESOLVER = "_resolve_secret"
_ENV_READ = re.compile(r"os\.(environ\.get|getenv|environ\[)")
#: An environment read that supplies a fallback: ``os.environ.get("X", "something")``.
_TWO_STATE_READ = re.compile(r"os\.(environ\.get|getenv)\([^)]*,")


def _source_lines() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#") or '"""' in line or "``" in line:
                continue  # prose about the rule is not a violation of it
            out.append((str(path.relative_to(_SRC)), number, line))
    return out


def test_only_the_resolver_reads_the_environment() -> None:
    reads = [(f, n, line.strip()) for f, n, line in _source_lines() if _ENV_READ.search(line)]
    assert reads, "the guard is vacuous if nothing reads the environment at all"
    resolver_body = _resolver_line_range()
    offenders = [
        f"{f}:{n}: {line}"
        for f, n, line in reads
        if not (f == "client.py" and resolver_body[0] <= n <= resolver_body[1])
    ]
    assert not offenders, (
        f"these lines read the environment outside client.{_RESOLVER}, so a credential can "
        "again be resolved in two states instead of three:\n" + "\n".join(offenders)
    )


def test_no_environment_read_carries_a_literal_default() -> None:
    """``os.environ.get(name, "")`` is the exact shape that reads absence as consent."""
    offenders = [
        f"{f}:{n}: {line.strip()}" for f, n, line in _source_lines() if _TWO_STATE_READ.search(line)
    ]
    assert not offenders, (
        "these environment reads supply a fallback, which collapses 'unset' and 'set to "
        "nothing' into one answer:\n" + "\n".join(offenders)
    )


def _resolver_line_range() -> tuple[int, int]:
    """First and last line of ``client._resolve_secret``, so the guard is scoped to its body."""
    lines = (_SRC / "client.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines, start=1) if line.startswith(f"def {_RESOLVER}("))
    end = next(
        (i for i, line in enumerate(lines[start:], start=start + 1) if line.startswith("def ")),
        len(lines),
    )
    return start, end
