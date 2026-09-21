from __future__ import annotations

import sys
from pathlib import Path

from memco import __all__ as KERNEL_EXPORTS
from memco.builtin import HashIndex, MemCache, SimpleTok, TemplateTeacher
from memco.bus import InprocBus
from memco.cloud import Cloud
from memco.config import MemoryConfig
from memco.digest import Turn, digest
from memco.edge import Edge
from memco.laya import LayaFlags, LayaGate, LayaHost
from memco.prefs import bullets, section
from memco.routers import NoopRouter


class Pred:
    def __init__(self, answers: dict, boom: bool = False) -> None:
        self.answers = answers
        self.boom = boom
        self.calls: list[tuple[object, dict]] = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        if self.boom:
            raise RuntimeError("laya down")
        return {"answers": self.answers}


def _pair(tmp: Path, gate) -> tuple[Edge, Cloud]:
    cfg = MemoryConfig(user_id="u1", short_cap=1)
    bus = InprocBus()
    edge = Edge(cfg, tmp / "edge", SimpleTok(), bus, fail=NoopRouter())
    cloud = Cloud(
        cfg,
        tmp / "cloud",
        MemCache(cfg.cache_cap, cfg.cache_ttl, cfg.cache_fill, cfg.cache_policy),
        HashIndex(),
        TemplateTeacher(),
        bus,
        gate=gate,
    )
    return edge, cloud


def test_kernel_surface_excludes_laya() -> None:
    assert "LayaHost" not in KERNEL_EXPORTS
    assert "LayaGate" not in KERNEL_EXPORTS
    assert "LayaFlags" not in KERNEL_EXPORTS


def test_tag_sets_five_flags() -> None:
    host = LayaHost(
        Pred(
            {
                "kind": {"choice": "commitment", "confidence": 0.94},
                "unresolved": {"noul": 0.91},
                "high_emotion": {"noul": 0.12},
                "need": {"noul": 0.88},
                "pref": {"noul": 0.05},
            }
        )
    )
    flags = host.tag("remind me to buy milk", "sure")
    assert flags.ok
    assert flags.kind == "commitment"
    assert flags.unresolved is True
    assert flags.high_emotion is False
    assert flags.need is True
    assert flags.pref is False
    turn = flags.turn("remind me to buy milk", "sure")
    assert turn == Turn("remind me to buy milk", "sure", kind="commitment", unresolved=True, high_emotion=False)
    pack = digest([turn], SimpleTok())
    assert any(b.kind == "commitment" and b.distill for b in pack.buckets)


def test_tag_low_confidence_stays_default() -> None:
    host = LayaHost(
        Pred(
            {
                "kind": {"choice": "feeling", "confidence": 0.4},
                "unresolved": {"noul": 0.5},
                "high_emotion": {"noul": 0.84},
                "need": {"noul": 0.2},
                "pref": {"noul": 0.849},
            }
        )
    )
    flags = host.tag("hello")
    assert flags.ok
    assert flags == LayaFlags(kind="event", ok=True, answers=flags.answers)


def test_tag_unknown_kind_is_event() -> None:
    host = LayaHost(Pred({"kind": {"choice": "topic", "confidence": 0.99}}))
    assert host.tag("x").kind == "event"


def test_tag_predict_error_is_noop() -> None:
    flags = LayaHost(Pred({}, boom=True)).tag("x")
    assert flags == LayaFlags()


def test_host_and_gate_share_one_load(monkeypatch) -> None:
    import memco.laya as adapt

    n = {"k": 0}

    class Fake:
        @staticmethod
        def load(model):
            n["k"] += 1
            return Pred({"drop": {"noul": 0.0}, "kind": {"choice": "event", "confidence": 0.99}})

    monkeypatch.setitem(sys.modules, "laya", Fake)
    monkeypatch.setattr(adapt, "_MODELS", {})
    LayaHost().tag("a")
    assert LayaGate().route("gate.t0", {"self_md": "x"}) is None
    assert n["k"] == 1


def test_tag_missing_sdk_is_noop(monkeypatch) -> None:
    import memco.laya as adapt

    def boom(_model: str):
        raise ImportError("no sdk")

    monkeypatch.setattr(adapt, "_load", boom)
    flags = LayaHost().tag("x")
    assert flags == LayaFlags()


def test_gate_drops_t0_and_distill() -> None:
    gate = LayaGate(Pred({"drop": {"noul": 0.96}}))
    dropped = gate.route("gate.t0", {"self_md": "# x", "user_md": "# y"})
    assert dropped is not None
    assert dropped.event == "laya.drop"
    assert gate.route("gate.distill", {"question": "q", "answer": "a", "source": "s"}) is not None
    assert gate.route("recall.empty", {"text": "x"}) is None


def test_gate_allows_low_noul() -> None:
    gate = LayaGate(Pred({"drop": {"noul": 0.1}}))
    assert gate.route("gate.t0", {"self_md": "ok"}) is None
    assert gate.route("gate.distill", {"question": "q", "answer": "a"}) is None


def test_gate_error_allows() -> None:
    gate = LayaGate(Pred({}, boom=True))
    assert gate.route("gate.t0", {"self_md": "x"}) is None


def test_laya_gate_drops_t0_writeback(tmp_path: Path) -> None:
    edge, cloud = _pair(tmp_path, LayaGate(Pred({"drop": {"noul": 0.99}})))
    edge.add(Turn("I like tea.", "ok"))
    edge.end_session()
    assert "tea" not in edge.user_md()
    assert bullets(section(edge.user_md(), "likes")) == []
    assert cloud.store.list_live()


def test_laya_gate_drops_distill_sample(tmp_path: Path) -> None:
    edge, cloud = _pair(tmp_path, LayaGate(Pred({"drop": {"noul": 0.99}})))
    edge.add(Turn("remind me to buy milk next time", "sure", kind="commitment", unresolved=True))
    edge.end_session()
    assert cloud.distill_path.read_text(encoding="utf-8") == ""


def test_laya_gate_allows_t0_when_open(tmp_path: Path) -> None:
    edge, _cloud = _pair(tmp_path, LayaGate(Pred({"drop": {"noul": 0.01}})))
    edge.add(Turn("I like tea.", "ok"))
    edge.end_session()
    assert any("tea" in x for x in bullets(section(edge.user_md(), "likes")))
