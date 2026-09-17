from __future__ import annotations

import time
from dataclasses import dataclass, field

from memco.bucket import Bucket, clip, new_id
from memco.plugins import Tokenizer
from memco.prefs import has_pref


@dataclass
class Turn:
    user: str
    agent: str = ""
    kind: str = "event"
    unresolved: bool = False
    high_emotion: bool = False


@dataclass
class Digest:
    stamp: str
    source: str
    buckets: list[Bucket] = field(default_factory=list)
    need_t0: bool = False


def source_text(turns: list[Turn]) -> str:
    return "\n".join(f"- user: {t.user}\n- agent: {t.agent}" for t in turns)


def user_lines(source: str) -> list[str]:
    out: list[str] = []
    for line in (source or "").splitlines():
        raw = line.strip()
        if raw.startswith("- user:"):
            text = raw[len("- user:") :].strip()
            if text:
                out.append(text)
    return out


def digest(turns: list[Turn], tok: Tokenizer, prior: set[str] | None = None, stamp: str | None = None) -> Digest:
    stamp = stamp or time.strftime("%Y-%m-%d %H:%M")
    src = source_text(turns)
    if not turns:
        return Digest(stamp=stamp, source="", need_t0=False)
    prior = set(prior or ())
    groups: dict[str, list[Turn]] = {"feeling": [], "commitment": [], "event": []}
    for turn in turns:
        kind = turn.kind if turn.kind in groups else "event"
        groups[kind].append(turn)
    buckets: list[Bucket] = []
    for kind, items in groups.items():
        if not items:
            continue
        if kind == "commitment":
            parts = [" ".join(x for x in (t.user, t.agent) if x).strip() for t in items]
            body = " ".join(p for p in parts if p)
        else:
            body = " ".join(t.user for t in items if t.user)
        if not body:
            continue
        kw = tok.pick(body)
        unresolved = any(t.unresolved for t in items)
        high = any(t.high_emotion for t in items)
        chatter = kind == "event" and not unresolved and not high and kw not in prior
        eligible = False
        if kind == "commitment":
            eligible = True
        elif kw and kw in prior:
            eligible = True
        elif unresolved or high:
            eligible = True
        if chatter:
            eligible = False
        buckets.append(
            Bucket(
                id=new_id(),
                kind=kind,
                stamp=stamp,
                keyword=kw,
                body=clip(body),
                unresolved=unresolved,
                high_emotion=high,
                distill=eligible,
            )
        )
    return Digest(
        stamp=stamp,
        source=src,
        buckets=buckets,
        need_t0=any(has_pref(t.user) for t in turns),
    )
