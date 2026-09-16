from __future__ import annotations

import json
from pathlib import Path

from memco.bucket import Bucket
from memco.config import UNLIMITED
from memco.digest import user_lines
from memco.plugins import Teacher
from memco.routers import Router, dispatch


def _hit(left: str, right: str) -> bool:
    a, b = (left or "").strip(), (right or "").strip()
    if not a or not b:
        return False
    return a in b or b in a


def questions(bucket: Bucket, source: str) -> list[str]:
    matched = [q for q in user_lines(source) if _hit(q, bucket.body) or _hit(q, bucket.keyword)]
    if matched:
        return matched
    if (bucket.body or "").strip():
        return [bucket.body.strip()]
    return []


def distill(
    buckets: list[Bucket],
    source: str,
    teacher: Teacher,
    gate: Router | None,
    limit: int,
    path: Path,
) -> Path:
    eligible = [b for b in buckets if b.distill and not b.archived]
    if limit != UNLIMITED:
        eligible = eligible[: max(0, int(limit))]
    samples: list[dict] = []
    seen: set[str] = _questions_on_disk(path)
    for bucket in eligible:
        for question in questions(bucket, source):
            q = question.strip()
            if not q or q in seen:
                continue
            out = teacher.complete("distill", {"question": q, "evidence": bucket.body})
            answer = str(out.get("answer") or "").strip()
            if not answer:
                continue
            blocked = dispatch(gate, "gate.distill", {"question": q, "answer": answer, "source": source})
            if blocked is not None:
                continue
            seen.add(q)
            samples.append({"messages": [{"role": "user", "content": q}, {"role": "assistant", "content": answer}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    if not samples:
        return path
    with path.open("a", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    return path


def _questions_on_disk(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        msgs = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(msgs, list) or not msgs:
            continue
        question = str((msgs[0] or {}).get("content") or "").strip()
        if question:
            out.add(question)
    return out
