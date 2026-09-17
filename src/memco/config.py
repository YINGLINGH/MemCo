from __future__ import annotations

from dataclasses import dataclass

POLICIES = ("lru", "lfu", "fifo", "random", "none")
UNLIMITED = -1


def at_cap(n: int, cap: int) -> bool:
    if cap == UNLIMITED:
        return False
    return int(n) >= int(cap)


def try_limit(retries: int) -> int | None:
    """None means retry forever. 0 still means one try."""
    if retries == UNLIMITED:
        return None
    return max(1, int(retries))


@dataclass
class MemoryConfig:
    long_cap: int = 1000
    short_cap: int = 50
    recall_boost: int = 1
    cache_ttl: int = 86400
    cache_cap: int = 1024
    cache_fill: float = 1.0
    cache_policy: str = "lru"
    n_tiers: int = 4
    forget_pct: int = 20
    tier_pcts: tuple[int, ...] = (1, 3, 6, 10)
    silence_sec: float = 10
    qos_recall: int = 1
    qos_archive: int = 1
    qos_snapshot: int = 1
    topic_prefix: str = "memco"
    shard_bytes: int = 262144
    net_timeout: float = 5
    net_retries: int = 3
    recall_limit: int = 3
    distill_limit: int = 3
    user_id: str = "user"

    def __post_init__(self) -> None:
        self.cache_policy = str(self.cache_policy or "lru").lower()
        if self.cache_policy not in POLICIES:
            raise ValueError(f"cache_policy must be one of {POLICIES}")
        self.n_tiers = max(1, int(self.n_tiers))
        self.tier_pcts = tuple(int(p) for p in self.tier_pcts)
        if len(self.tier_pcts) != self.n_tiers:
            raise ValueError("len(tier_pcts) must equal n_tiers")
        if sum(self.tier_pcts) != int(self.forget_pct):
            raise ValueError("sum(tier_pcts) must equal forget_pct")
        for name in ("qos_recall", "qos_archive", "qos_snapshot"):
            qos = int(getattr(self, name))
            if qos not in (0, 1, 2):
                raise ValueError(f"{name} must be 0, 1, or 2")
        prefix = str(self.topic_prefix or "").strip()
        if not prefix or "/" in prefix:
            raise ValueError("topic_prefix must be a short name without '/'")
        self.topic_prefix = prefix
        uid = str(self.user_id or "").strip()
        if not uid or "/" in uid:
            raise ValueError("user_id must be a short name without '/'")
        self.user_id = uid

    def clip(self, n: int) -> int | None:
        """None means no limit."""
        if n == UNLIMITED:
            return None
        return max(1, int(n))

    def topic(self, kind: str) -> str:
        return f"{self.topic_prefix}/{self.user_id}/{kind}"
