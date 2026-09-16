from __future__ import annotations

import json
from pathlib import Path

from memco.bucket import Bucket, sha256_text
from memco.config import MemoryConfig
from memco.distill import distill
from memco.forget import forget_if_at_cap
from memco.plugins import Bus, Cache, Teacher, VectorIndex
from memco.recall import cloud_recall
from memco.routers import Router, dispatch
from memco.store import Store


class Cloud:
    def __init__(
        self,
        cfg: MemoryConfig,
        root: Path,
        cache: Cache,
        index: VectorIndex,
        teacher: Teacher,
        bus: Bus,
        gate: Router | None = None,
        vec_fail: Router | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache = cache
        self.index = index
        self.teacher = teacher
        self.bus = bus
        self.gate = gate
        self.vec_fail = vec_fail
        self.root = Path(root)
        self.store = Store(self.root / "long")
        self.seen = self.root / "seen"
        self.seen.mkdir(parents=True, exist_ok=True)
        self.distill_path = self.root / "distill.jsonl"
        bus.subscribe(cfg.topic("recall/req"), self._on_recall)
        bus.subscribe(cfg.topic("archive"), self._on_archive)

    def _on_recall(self, payload: dict) -> None:
        result, _route = cloud_recall(
            self.cache,
            self.index,
            self.store,
            self.cfg,
            str(payload.get("keyword") or ""),
            payload.get("referred"),
            vec_router=self.vec_fail,
        )
        self.bus.publish(
            self.cfg.topic("recall/res"),
            {
                "hits": result.hits,
                "source": result.source,
                "vec_down": result.vec_down,
                "rid": payload.get("rid"),
            },
        )

    def _on_archive(self, payload: dict) -> None:
        self.ingest(payload)

    def ingest(self, payload: dict) -> list[str]:
        key = _batch_key(payload)
        mark = self.seen / key
        if mark.is_file():
            return []
        source = str(payload.get("source") or payload.get("source_text") or "")
        if source:
            self.store.write_source(source)
        raw = payload.get("buckets") or []
        buckets = [Bucket.from_dict(item) for item in raw]
        for bucket in buckets:
            self.store.write(bucket)
            try:
                self.cache.set_item(self.cfg.user_id, bucket.stamp, bucket.keyword, json.dumps(bucket.to_dict(), ensure_ascii=False))
            except Exception:
                pass
            try:
                ok = self.index.add(bucket.keyword, bucket.body)
                if ok is False:
                    dispatch(self.vec_fail, "vec.down", {"keyword": bucket.keyword})
            except Exception:
                dispatch(self.vec_fail, "vec.down", {"keyword": bucket.keyword})
        before = {b.id: b for b in self.store.list_live()}
        dropped = forget_if_at_cap(
            self.store,
            self.cfg.long_cap,
            self.cfg.n_tiers,
            self.cfg.forget_pct,
            self.cfg.tier_pcts,
        )
        for bucket_id in dropped:
            gone = before.get(bucket_id)
            if gone is None:
                continue
            try:
                self.cache.delete_item(self.cfg.user_id, gone.stamp, gone.keyword)
            except Exception:
                pass
        if dropped:
            try:
                self.index.rebuild(self.store.list_live())
            except Exception:
                pass
        self.store.gc_sources()
        distill(buckets, source, self.teacher, self.gate, self.cfg.distill_limit, self.distill_path)
        t0 = self.teacher.complete(
            "t0",
            {
                "source": source,
                "self_md": payload.get("self_md") or "",
                "user_md": payload.get("user_md") or "",
            },
        )
        blocked = dispatch(self.gate, "gate.t0", t0)
        if blocked is None:
            self.bus.publish(self.cfg.topic("t0"), t0)
        mark.write_text("1\n", encoding="utf-8")
        return dropped


def _batch_key(payload: dict) -> str:
    ids: list[str] = []
    for item in payload.get("buckets") or []:
        if isinstance(item, dict):
            bucket_id = str(item.get("id") or "").strip()
            if bucket_id:
                ids.append(bucket_id)
    blob = json.dumps(
        {"ids": sorted(ids), "source": str(payload.get("source") or payload.get("source_text") or "")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256_text(blob)
