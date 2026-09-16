from __future__ import annotations

import json
from pathlib import Path

from memco.bucket import Bucket, dumps, loads
from memco.builtin import HashIndex, MemCache, SimpleTok, TemplateTeacher
from memco.bus import InprocBus, pack_parts, take_part
from memco.cloud import Cloud
from memco.config import UNLIMITED, MemoryConfig, at_cap, try_limit
from memco.digest import Turn
from memco.edge import Edge
from memco.forget import forget_if_at_cap, plan_forget
from memco.prefs import EMPTY_SELF, EMPTY_USER, bullets, has_pref, lookup, section
from memco.rank import rank
from memco.recall import cloud_recall, merge
from memco.routers import NoopRouter, Route
from memco.store import Store


def _pair(tmp: Path, **kw) -> tuple[Edge, Cloud, InprocBus]:
    cfg = MemoryConfig(user_id="u1", **kw)
    bus = InprocBus()
    edge = Edge(cfg, tmp / "edge", SimpleTok(), bus, fail=NoopRouter())
    cloud = Cloud(
        cfg,
        tmp / "cloud",
        MemCache(cfg.cache_cap, cfg.cache_ttl, cfg.cache_fill, cfg.cache_policy),
        HashIndex(),
        TemplateTeacher(),
        bus,
        gate=NoopRouter(),
    )
    return edge, cloud, bus


def test_short_not_t0_until_upload(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=20)
    edge.add(Turn("I like tea.", "Okay."))
    edge.end_session()
    assert "tea" not in edge.user_md()
    assert edge.store.list_live()


def test_upload_writes_t0_and_drops_short(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("I like tea.", "Okay."))
    edge.end_session()
    assert any("tea" in x for x in bullets(section(edge.user_md(), "likes")))
    assert edge.store.list_live() == []
    assert cloud.store.list_live()


def test_short_cap_unlimited_no_upload(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=UNLIMITED)
    edge.add(Turn("I like tea.", "Okay."))
    edge.end_session()
    assert cloud.store.list_live() == []
    assert "tea" not in edge.user_md()


def test_offline_random_forget(tmp_path: Path) -> None:
    edge, _cloud, bus = _pair(tmp_path, short_cap=1)
    bus.up = False
    edge.add(Turn("keep this", "ok", kind="event"))
    edge.end_session()
    assert len(edge.store.list_live()) == 1
    edge.add(Turn("new one", "ok"))
    edge.end_session()
    assert len(edge.store.list_live()) == 1


def test_recall_degrade(tmp_path: Path) -> None:
    edge, _cloud, bus = _pair(tmp_path, short_cap=20)
    edge.add(Turn("the lamp broke", "noted"))
    edge.end_session()
    bus.up = False
    hit, _ = edge.recall("the lamp", need=True, pref=False, keyword="lamp", referred=None)
    assert hit.source in {"short", "net_down"}
    miss, route = edge.recall("yesterday's bow", need=True, pref=False, keyword="bow", referred="yesterday")
    assert miss.source == "net_down"
    assert not miss.hits


def test_empty_fail_custom(tmp_path: Path) -> None:
    class R:
        def route(self, event, payload):
            return Route(event="host.miss")

    cfg = MemoryConfig(user_id="u1", short_cap=20)
    bus = InprocBus()
    bus.up = False
    edge = Edge(cfg, tmp_path / "e", SimpleTok(), bus, fail=R())
    _hit, route = edge.recall("yesterday's bow", need=True, pref=False, keyword="bow", referred="yesterday")
    assert route and route.event == "host.miss"


def test_distill_user_question(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("remind me to buy milk next time", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    text = cloud.distill_path.read_text(encoding="utf-8")
    assert "remind me to buy milk next time" in text
    assert "do you remember" not in text.lower()


def test_chatter_not_distilled(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("hello", "hi"))
    edge.end_session()
    text = cloud.distill_path.read_text(encoding="utf-8")
    assert "hello" not in text


def test_forget_clamp_and_ceil() -> None:
    buckets = [
        Bucket(id=f"b{i:02d}", kind="event", stamp="d", keyword="k", body=f"n{i}", weight=100 - i)
        for i in range(10)
    ]
    ids = plan_forget(
        buckets,
        n_tiers=4,
        forget_pct=20,
        tier_pcts=(1, 3, 6, 10),
        long_cap=UNLIMITED,
        rng=__import__("random").Random(0),
    )
    assert len(ids) == 4
    assert len(set(ids)) == len(ids)


def test_forget_uses_capacity_not_live_n() -> None:
    buckets = [
        Bucket(id=f"b{i:02d}", kind="event", stamp="d", keyword="k", body=f"n{i}", weight=100 - i)
        for i in range(10)
    ]
    ids = plan_forget(
        buckets,
        n_tiers=4,
        forget_pct=20,
        tier_pcts=(1, 3, 6, 10),
        long_cap=100,
        rng=__import__("random").Random(0),
    )
    assert len(ids) == 9


def test_long_cap_unlimited_no_forget(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1, long_cap=UNLIMITED, forget_pct=20)
    for i in range(5):
        edge.add(Turn(f"note {i} about lamp", "ok", unresolved=True))
        edge.end_session()
    assert len(cloud.store.list_live()) == 5


def test_gate_can_drop_t0(tmp_path: Path) -> None:
    class Block:
        def route(self, event, payload):
            if event == "gate.t0":
                return Route(event="drop")
            return None

    cfg = MemoryConfig(user_id="u1", short_cap=1)
    bus = InprocBus()
    edge = Edge(cfg, tmp_path / "e", SimpleTok(), bus)
    Cloud(cfg, tmp_path / "c", MemCache(8, 60, 1.0, "lru"), HashIndex(), TemplateTeacher(), bus, gate=Block())
    edge.add(Turn("I like tea.", "ok"))
    edge.end_session()
    assert "tea" not in edge.user_md()


def test_silence_tick(tmp_path: Path) -> None:
    edge, _c, _b = _pair(tmp_path, short_cap=20, silence_sec=5)
    edge.add(Turn("hello", "hi"))
    edge.idle(now=0)
    assert not edge.tick(now=4)
    assert edge.tick(now=5)
    assert edge.turns == []


def test_manual_end_event(tmp_path: Path) -> None:
    seen: list[str] = []

    class End:
        def route(self, event, payload):
            seen.append(event)
            return Route(event="host.stop")

    cfg = MemoryConfig(user_id="u1", silence_sec=0, short_cap=20)
    bus = InprocBus()
    edge = Edge(cfg, tmp_path / "e", SimpleTok(), bus, end=End())
    edge.add(Turn("hello", "hi"))
    edge.end_session()
    assert "session.end" in seen


def test_cache_fill() -> None:
    cache = MemCache(cap=4, ttl=UNLIMITED, fill=0.5, policy="lru")
    for i in range(4):
        cache.set_item("u", f"t{i}", f"k{i}", f"v{i}")
    assert cache.get_by_keyword("u", "k0") is None
    raw = cache.get_by_keyword("u", "k3")
    assert raw is not None
    assert json.loads(raw)["text"] == "v3"


def test_config_tier_sum() -> None:
    try:
        MemoryConfig(n_tiers=4, forget_pct=20, tier_pcts=(1, 1, 1, 1))
    except ValueError:
        return
    raise AssertionError("expected sum mismatch")


def test_long_forget_until_under_cap(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1, long_cap=5)
    for i in range(12):
        edge.add(Turn(f"note {i} about lamp{i}", "ok", unresolved=True))
        edge.end_session()
    assert len(cloud.store.list_live()) < 5


def test_session_uploads_together(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("hello there", "hi"))
    edge.add(Turn("remind me to buy milk", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    assert edge.store.list_live() == []
    kinds = {b.kind for b in cloud.store.list_live()}
    assert "commitment" in kinds
    assert "event" in kinds


def test_distill_appends(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("remind me to buy milk", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    edge.add(Turn("remind me to call mom", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    text = cloud.distill_path.read_text(encoding="utf-8")
    assert "buy milk" in text
    assert "call mom" in text


def test_index_no_prefix_hit() -> None:
    index = HashIndex()
    index.add("bar10", "keep bar10")
    hits, _ = index.query("bar1", top_k=8)
    assert hits == []
    index.add("bar1", "keep bar1")
    hits, _ = index.query("bar1", top_k=8)
    assert "keep bar1" in hits
    assert "keep bar10" not in hits


def test_mqtt_parts_roundtrip() -> None:
    body = json.dumps({"path": "x" * 40, "n": 1}, ensure_ascii=False)
    parts = pack_parts(body, 20)
    assert len(parts) > 1
    buf: dict = {}
    out = None
    for raw in parts:
        out = take_part(json.loads(raw), buf)
    assert out == json.loads(body)


def test_try_limit_and_at_cap() -> None:
    assert try_limit(0) == 1
    assert try_limit(3) == 3
    assert try_limit(UNLIMITED) is None
    assert at_cap(0, 0)
    assert not at_cap(9, UNLIMITED)
    assert at_cap(1, 1)


def test_pref_words_not_substrings() -> None:
    assert not has_pref("that is likely")
    assert has_pref("I like tea")
    assert lookup(EMPTY_SELF, EMPTY_USER, "remember this memory") == []


def test_empty_t0_still_writes(tmp_path: Path) -> None:
    edge, _cloud, _bus = _pair(tmp_path, short_cap=20)
    edge._on_t0({"self_md": "", "user_md": None})
    assert edge.self_md().strip() == ""
    assert "User preference" in edge.user_md()


def test_forget_large_overshoot(tmp_path: Path) -> None:
    cfg = MemoryConfig(user_id="u1", long_cap=5)
    store = Store(tmp_path / "long")
    for i in range(80):
        store.write(Bucket(id=f"{i:04d}", kind="event", stamp="d", keyword=f"k{i}", body=f"note {i}"))
    forget_if_at_cap(store, cfg.long_cap, cfg.n_tiers, cfg.forget_pct, cfg.tier_pcts)
    assert len(store.list_live()) < 5


def test_cache_keeps_sibling_after_delete() -> None:
    cache = MemCache(cap=8, ttl=UNLIMITED, fill=1.0, policy="lru")
    cache.set_item("u", "t", "k1", "alpha")
    cache.set_item("u", "t", "k2", "beta")
    cache.delete_item("u", "t", "k2")
    raw = cache.get_by_time("u", "t")
    assert raw is not None
    assert json.loads(raw)["text"] == "alpha"


def test_loads_keeps_leading_dash() -> None:
    bucket = Bucket(id="dash1", kind="event", stamp="t", keyword="k", body="- dash start")
    got = loads(dumps(bucket))
    assert got.body.startswith("-")


def test_time_recall_keeps_promise() -> None:
    bucket = Bucket(
        id="p1",
        kind="commitment",
        stamp="2020-01-01 10:00",
        keyword="milk",
        body="buy milk",
        unresolved=True,
    )
    hits = rank([bucket], keyword="", referred="2020-01-01", limit=3)
    assert [b.id for b in hits] == ["p1"]


def test_merge_drops_vector_duplicate() -> None:
    stored = Bucket(id="s1", kind="event", stamp="t", keyword="lamp", body="the lamp broke")
    vec = Bucket(id="v1", kind="event", stamp="", keyword="lamp", body="the lamp broke")
    out = merge([stored, vec])
    assert len(out) == 1
    assert out[0].id == "s1"


def test_distill_limit_zero(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1, distill_limit=0)
    edge.add(Turn("remind me to buy milk", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    assert cloud.distill_path.read_text(encoding="utf-8") == ""


def test_cache_keeps_kind(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("remind me to buy milk", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    live = cloud.store.list_live()
    assert live
    stamp = live[0].stamp
    result, _ = cloud_recall(cloud.cache, cloud.index, cloud.store, cloud.cfg, "", stamp)
    assert result.source == "cache-time"
    assert result.hits
    assert "promise" in result.hits[0]


def test_time_recall_all_same_stamp(tmp_path: Path) -> None:
    edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    edge.add(Turn("the lamp broke", "noted"))
    edge.add(Turn("remind me to buy milk", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    stamp = cloud.store.list_live()[0].stamp
    result, _ = cloud_recall(cloud.cache, cloud.index, cloud.store, cloud.cfg, "", stamp)
    assert len(result.hits) == 2
    blob = " ".join(result.hits)
    assert "lamp" in blob
    assert "milk" in blob


def test_offline_forget_drops_source(tmp_path: Path) -> None:
    edge, _cloud, bus = _pair(tmp_path, short_cap=1)
    bus.up = False
    edge.add(Turn("keep this lamp", "ok", unresolved=True))
    edge.end_session()
    assert list(edge.store.sources.glob("*.md"))
    edge.add(Turn("new one about tea", "ok", unresolved=True))
    edge.end_session()
    live = edge.store.list_live()
    assert len(live) == 1
    keep = {b.source_sha256 for b in live if b.source_sha256}
    leftover = [p for p in edge.store.sources.glob("*.md") if p.stem not in keep]
    assert leftover == []


def test_archive_redelivery_is_idempotent(tmp_path: Path) -> None:
    _edge, cloud, _bus = _pair(tmp_path, short_cap=1)
    bucket = Bucket(
        id="a1b2c3d4e5f6",
        kind="commitment",
        stamp="2020-01-01 10:00",
        keyword="milk",
        body="remind me to buy milk",
        unresolved=True,
        distill=True,
    )
    payload = {
        "source": "- user: remind me to buy milk\n- agent: sure",
        "buckets": [bucket.to_dict()],
        "self_md": "",
        "user_md": "",
    }
    cloud.ingest(payload)
    lines = [ln for ln in cloud.distill_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    n_live = len(cloud.store.list_live())
    n_index = len(cloud.index.keys)
    assert cloud.ingest(payload) == []
    assert len(cloud.store.list_live()) == n_live
    assert len(cloud.index.keys) == n_index
    again = [ln for ln in cloud.distill_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert again == lines
