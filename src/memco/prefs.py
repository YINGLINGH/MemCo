from __future__ import annotations

import re

EMPTY_SELF = """# Self preference

## likes

(empty)

## dislikes

(empty)
"""

EMPTY_USER = """# User preference

## likes

(empty)

## dislikes

(empty)
"""

_LIKE = ("like", "love", "prefer", "enjoy")
_HATE = ("hate", "dislike")


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in words)


def section(md: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\s*$"
    lines = md.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line.strip(), flags=re.I):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def bullets(md_section: str) -> list[str]:
    items: list[str] = []
    for line in md_section.splitlines():
        raw = line.strip()
        if raw.startswith("- "):
            raw = raw[2:].strip()
        if not raw or raw.lower() in {"(empty)", "(none)"} or raw.startswith("#"):
            continue
        items.append(raw)
    return items


def lookup(self_md: str, user_md: str, text: str) -> list[str]:
    if not _has_word(text, _LIKE + _HATE):
        return []
    want_hate = _has_word(text, _HATE)
    want_like = _has_word(text, _LIKE) and not _has_word(text, ("dislike",))
    if not want_like and not want_hate:
        want_like = want_hate = True
    want_self = _has_word(text, ("you", "your", "self"))
    want_user = _has_word(text, ("i", "my", "me", "mine"))
    if not want_self and not want_user:
        want_self = want_user = True
    hits: list[str] = []
    if want_self:
        if want_like:
            hits.extend(f"self like: {x}" for x in bullets(section(self_md, "likes")))
        if want_hate:
            hits.extend(f"self dislike: {x}" for x in bullets(section(self_md, "dislikes")))
    if want_user:
        if want_like:
            hits.extend(f"user like: {x}" for x in bullets(section(user_md, "likes")))
        if want_hate:
            hits.extend(f"user dislike: {x}" for x in bullets(section(user_md, "dislikes")))
    return hits


def has_pref(text: str) -> bool:
    return _has_word(text, _LIKE + _HATE)
