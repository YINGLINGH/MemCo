from __future__ import annotations

import json
from dataclasses import dataclass, field

from memco.bucket import Bucket
from memco.builtin import bounded
from memco.config import UNLIMITED, MemoryConfig
from memco.forget import bump
from memco.plugins import Cache, VectorIndex
from memco.rank import rank
from memco.routers import Route, Router, dispatch
from memco.store import Store

_KW_STAMP = "\x00k"
_QSEP = "\x1f"
_EXCERPT_PAIRS = 2


@dataclass
class Recall:
    hits: list[str] = field(default_factory=list)
    source: str = "none"
    vec_down: bool = False


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


def format_hit(store: Store, bucket: Bucket, keyword: str = "") -> str:
    base = bucket.line()
    if bucket.kind != "event":
        return base
    excerpt = source_excerpt(store, bucket, keyword=keyword)
    if not excerpt:
        return base
    return f"{base}\n{excerpt}"


def source_excerpt(store: Store, bucket: Bucket, n: int = _EXCERPT_PAIRS, keyword: str = "") -> str:
    text = store.read_source(bucket.source_sha256)
    pairs = _pairs(text)
    if not pairs:
        return ""
    body = bucket.body or ""
    start = 0
    needle = (keyword or "").strip()
    if needle:
        for i, (user, agent) in enumerate(pairs):
            blob = f"{user} {agent}"
            if bounded(needle, blob) or needle in blob:
                start = i
                break
    else:
        for i, (user, _agent) in enumerate(pairs):
            if user and (body == user or body in user):
                start = i
                break
    chunk = pairs[start : start + max(1, int(n))]
    lines: list[str] = []
    for user, agent in chunk:
        if user:
            lines.append(f"- user: {user}")
        if agent:
            lines.append(f"- agent: {agent}")
    return "\n".join(lines)


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
    referred = (referred or "").strip() or None
    extra: Route | None = None
    degraded = not index.ok
    if degraded:
        extra = dispatch(vec_router, "vec.down", {"keyword": keyword})
    qkey = _qkey(keyword, referred)
    packed = _cached_result(cache.get_by_keyword(cfg.user_id, qkey))
    if packed:
        _boost_ids(store, packed.get("ids") or [], cfg.recall_boost)
        source = "cache-time" if referred else "cache-kw"
        return Recall(hits=list(packed["hits"]), source=source, vec_down=bool(packed.get("vec_down")) or degraded), extra
    if referred:
        timed = _find(store, referred_time=referred)
        ranked = rank(merge(list(timed)), keyword, referred, limit=cfg.recall_limit)
        if ranked:
            bump(store, ranked, cfg.recall_boost)
            hits = [format_hit(store, b, keyword) for b in ranked]
            _save_result(cache, cfg, referred, qkey, hits, [b.id for b in ranked if b.id], False)
            return Recall(hits=hits, source="db-time"), extra
    if keyword:
        exact = _find(store, keyword=keyword)
        top = UNLIMITED if cfg.recall_limit == UNLIMITED else max(int(cfg.recall_limit), 1)
        vec_texts, vec_down = index.query(keyword, top_k=top)
        if vec_down:
            extra = extra or dispatch(vec_router, "vec.down", {"keyword": keyword})
        vec_buckets = [Bucket.from_dict({"keyword": keyword, "body": text, "kind": "event"}) for text in vec_texts]
        merged = merge([*exact, *vec_buckets])
        ranked = rank(merged, keyword, referred, limit=cfg.recall_limit)
        if ranked:
            bump(store, ranked, cfg.recall_boost)
            hits = [format_hit(store, b, keyword) for b in ranked]
            source = "db-kw" if vec_down else "db-kw-vec"
            _save_result(cache, cfg, _KW_STAMP, qkey, hits, [b.id for b in ranked if b.id], degraded or vec_down)
            return Recall(hits=hits, source=source, vec_down=degraded or vec_down), extra
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


def _qkey(keyword: str, referred: str | None) -> str:
    return f"{referred or ''}{_QSEP}{keyword or ''}"


def _pairs(text: str) -> list[tuple[str, str]]:
    user = ""
    agent = ""
    out: list[tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("- user:"):
            if user or agent:
                out.append((user, agent))
            user = line[len("- user:") :].strip()
            agent = ""
        elif line.startswith("- agent:"):
            agent = line[len("- agent:") :].strip()
    if user or agent:
        out.append((user, agent))
    return out


def _cached_result(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    hits = data.get("hits")
    if not isinstance(hits, list) or not hits:
        return None
    if not all(isinstance(item, str) for item in hits):
        return None
    return data


def _save_result(
    cache: Cache,
    cfg: MemoryConfig,
    stamp: str,
    keyword: str,
    hits: list[str],
    ids: list[str],
    vec_down: bool,
) -> None:
    try:
        cache.set_item(
            cfg.user_id,
            stamp,
            keyword,
            json.dumps({"hits": hits, "ids": ids, "vec_down": bool(vec_down)}, ensure_ascii=False),
        )
    except Exception:
        pass


def _boost_ids(store: Store, ids: list, boost: int) -> None:
    want = {str(i) for i in ids if i}
    if not want or int(boost) == 0:
        return
    ranked = [b for b in store.list_live() if b.id in want]
    if ranked:
        bump(store, ranked, boost)


def is_empty(need_recall: bool, result: Recall) -> bool:
    if not need_recall:
        return False
    if result.hits:
        return False
    if result.source == "t0":
        return False
    return True
