from __future__ import annotations

from dataclasses import dataclass, field

from memco.bucket import Bucket
from memco.builtin import bounded
from memco.config import UNLIMITED, MemoryConfig
from memco.forget import bump
from memco.plugins import Cache, VectorIndex
from memco.rank import rank
from memco.routers import Route, Router, dispatch
from memco.store import Store


@dataclass
class Recall:
    hits: list[str] = field(default_factory=list)
    source: str = "none"
    vec_down: bool = False


def from_cache(raw: dict | None) -> Bucket | None:
    if not raw:
        return None
    return Bucket.from_dict(raw if raw.get("kind") or raw.get("id") else {
        "stamp": raw.get("stamp") or "",
        "keyword": raw.get("keyword") or "",
        "body": raw.get("text") or raw.get("body") or "",
        "kind": "event",
    })


def merge(items: list[Bucket | None]) -> list[Bucket]:
    out: list[Bucket] = []
    seen_id: set[str] = set()
    seen_body: set[str] = set()
    for bucket in items:
        if bucket is None or not bucket.body:
            continue
        body_key = f"{bucket.keyword}|{bucket.body}"
        if bucket.id and bucket.id in seen_id:
            continue
        if body_key in seen_body:
            continue
        if bucket.id:
            seen_id.add(bucket.id)
        seen_body.add(body_key)
        out.append(bucket)
    return out


def local_recall(store: Store, keyword: str, referred: str | None, limit: int) -> list[Bucket]:
    return rank(store.list_live(), keyword, referred, limit=limit)


def cloud_recall(
    cache: Cache,
    index: VectorIndex,
    store: Store,
    cfg: MemoryConfig,
    keyword: str,
    referred: str | None,
    tok_router: Router | None = None,
    vec_router: Router | None = None,
) -> tuple[Recall, Route | None]:
    keyword = (keyword or "").strip()
    extra: Route | None = None
    degraded = not index.ok
    if degraded:
        extra = dispatch(vec_router, "vec.down", {"keyword": keyword})
    if referred:
        cached = from_cache(_parse(cache.get_by_time(cfg.user_id, referred)))
        timed = _find(store, referred_time=referred)
        ranked = rank(merge([cached, *timed]), keyword, referred, limit=cfg.recall_limit)
        if ranked:
            bump(store, ranked, cfg.recall_boost)
            source = "cache-time" if cached else "db-time"
            return Recall(hits=[b.line() for b in ranked], source=source), extra
    if keyword:
        cached = from_cache(_parse(cache.get_by_keyword(cfg.user_id, keyword)))
        exact = _find(store, keyword=keyword)
        top = UNLIMITED if cfg.recall_limit == UNLIMITED else max(int(cfg.recall_limit), 1)
        vec_texts, vec_down = index.query(keyword, top_k=top)
        if vec_down:
            extra = extra or dispatch(vec_router, "vec.down", {"keyword": keyword})
        vec_buckets = [Bucket.from_dict({"keyword": keyword, "body": text, "kind": "event"}) for text in vec_texts]
        merged = merge([cached, *exact, *vec_buckets])
        ranked = rank(merged, keyword, referred, limit=cfg.recall_limit)
        if ranked:
            bump(store, ranked, cfg.recall_boost)
            source = "cache-kw" if cached else "db-kw-vec"
            if vec_down and not cached:
                source = "db-kw"
            return Recall(hits=[b.line() for b in ranked], source=source, vec_down=degraded or vec_down), extra
    return Recall(hits=[], source="miss", vec_down=degraded), extra


def _find(store: Store, keyword: str | None = None, referred_time: str | None = None) -> list[Bucket]:
    keyword = (keyword or "").strip()
    referred = (referred_time or "").strip()
    hits: list[Bucket] = []
    for bucket in store.list_live():
        blob = f"{bucket.stamp} {bucket.keyword} {bucket.body}"
        time_ok = (not referred) or referred in blob
        kw_ok = (not keyword) or keyword == bucket.keyword or bounded(keyword, blob)
        if referred and keyword:
            if time_ok or kw_ok:
                hits.append(bucket)
        elif referred and time_ok:
            hits.append(bucket)
        elif keyword and not referred and kw_ok:
            hits.append(bucket)
    return hits


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    import json

    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"text": raw}
    except Exception:
        return {"text": raw}


def is_empty(need_recall: bool, result: Recall) -> bool:
    if not need_recall:
        return False
    if result.hits:
        return False
    if result.source == "t0":
        return False
    return True
