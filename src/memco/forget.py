from __future__ import annotations

import math
import random
from typing import Protocol

from memco.bucket import Bucket
from memco.config import UNLIMITED, at_cap


class LongStore(Protocol):
    def list_live(self) -> list[Bucket]: ...

    def write(self, bucket: Bucket) -> object: ...

    def delete(self, bucket_id: str) -> object: ...


def split_tiers(buckets: list[Bucket], n_tiers: int) -> list[list[Bucket]]:
    live = [b for b in buckets if not b.archived]
    ordered = sorted(live, key=lambda b: (-int(b.weight), b.id))
    n = len(ordered)
    k = max(1, int(n_tiers))
    return [ordered[(i * n) // k : ((i + 1) * n) // k] for i in range(k)]


def plan_forget(
    buckets: list[Bucket],
    n_tiers: int,
    forget_pct: int,
    tier_pcts: tuple[int, ...],
    long_cap: int = UNLIMITED,
    rng: random.Random | None = None,
) -> list[str]:
    if int(forget_pct) <= 0:
        return []
    live = [b for b in buckets if not b.archived]
    n = len(live)
    if n == 0:
        return []
    base = int(long_cap) if long_cap != UNLIMITED and int(long_cap) > 0 else n
    picker = rng if rng is not None else random
    chosen: list[str] = []
    for tier, pct in zip(split_tiers(live, n_tiers), tier_pcts):
        if not tier or int(pct) <= 0:
            continue
        quota = min(len(tier), math.ceil(base * int(pct) / 100.0))
        if quota <= 0:
            continue
        pool = list(tier)
        picker.shuffle(pool)
        chosen.extend(b.id for b in pool[:quota])
    return chosen


def forget_if_at_cap(
    store: LongStore,
    long_cap: int,
    n_tiers: int,
    forget_pct: int,
    tier_pcts: tuple[int, ...],
    rng: random.Random | None = None,
) -> list[str]:
    if long_cap == UNLIMITED:
        return []
    dropped: list[str] = []
    while True:
        live = store.list_live()
        if not at_cap(len(live), long_cap):
            break
        ids = plan_forget(live, n_tiers, forget_pct, tier_pcts, long_cap=long_cap, rng=rng)
        if not ids:
            break
        before = len(live)
        for bucket_id in ids:
            store.delete(bucket_id)
            dropped.append(bucket_id)
        if len(store.list_live()) >= before:
            break
    return dropped


def bump(store: LongStore, ranked: list[Bucket], boost: int) -> None:
    if int(boost) == 0:
        return
    live = store.list_live()
    by_id = {b.id: b for b in live}
    seen: set[str] = set()
    for hit in ranked:
        current = by_id.get(hit.id)
        if current is None:
            for bucket in live:
                if bucket.keyword == hit.keyword and bucket.body == hit.body:
                    current = bucket
                    break
        if current is None or current.id in seen:
            continue
        seen.add(current.id)
        current.weight = int(current.weight) + int(boost)
        store.write(current)
        by_id[current.id] = current
