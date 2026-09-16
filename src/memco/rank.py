from __future__ import annotations

from memco.bucket import Bucket
from memco.builtin import bounded
from memco.config import UNLIMITED


def rank(
    buckets: list[Bucket],
    keyword: str,
    referred: str | None,
    limit: int = 3,
) -> list[Bucket]:
    keyword = (keyword or "").strip()
    time_key = (referred or "").strip()
    scored: list[tuple[float, Bucket]] = []
    for bucket in buckets:
        if bucket.archived:
            continue
        time_hit = bool(time_key and time_key in f"{bucket.stamp} {bucket.body}")
        kw_hit = bool(keyword and (keyword == bucket.keyword or bounded(keyword, bucket.body)))
        if not kw_hit and not time_hit:
            continue
        score = 0.0
        if time_hit:
            score += 100.0
        if kw_hit:
            score += 40.0
        if bucket.unresolved:
            score += 30.0
        if bucket.high_emotion:
            score += 20.0
        if bucket.kind == "event":
            score += 1.0
        score += min(bucket.weight, 5) * 0.1
        scored.append((score, bucket))
    scored.sort(key=lambda item: (-item[0], item[1].stamp))
    ranked = [bucket for _, bucket in scored]
    if limit == UNLIMITED:
        return ranked
    return ranked[: max(0, int(limit))]
