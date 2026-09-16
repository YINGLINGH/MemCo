from __future__ import annotations

import random
from collections.abc import Callable
from typing import Protocol

from memco.bucket import Bucket
from memco.config import at_cap


class ShortStore(Protocol):
    def list_live(self) -> list[Bucket]: ...

    def delete(self, bucket_id: str) -> object: ...


def forget_random(store: ShortStore, rng: random.Random | None = None) -> str | None:
    live = [b for b in store.list_live() if not b.archived]
    if not live:
        return None
    picker = rng if rng is not None else random
    victim = picker.choice(live)
    store.delete(victim.id)
    return victim.id


def make_room(
    store: ShortStore,
    short_cap: int,
    can_upload: bool,
    upload: Callable[[], bool],
    rng: random.Random | None = None,
) -> str:
    if not at_cap(len(store.list_live()), short_cap):
        return ""
    if can_upload and upload():
        return "upload"
    if forget_random(store, rng=rng):
        return "forget"
    return ""
