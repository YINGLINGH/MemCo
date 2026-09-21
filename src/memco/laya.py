"""Optional Laya adapter. Not part of the memory kernel.

Host fills Turn flags and recall need/pref before calling Edge.
Cloud.gate can be LayaGate for gate.t0 and gate.distill.
Missing extra, load failure, or a dead predict all no-op: flags stay
Turn defaults, gates return None so MemCo runs as if Laya were absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memco.digest import Turn
from memco.routers import Route

DEFAULT_MODEL = "convaiinnovations/laya"
DEFAULT_THRESHOLD = 0.85
KINDS = ("event", "feeling", "commitment")
_MODELS: dict[str, Any] = {}

TAG_QUESTIONS = {
    "kind": {
        "type": "choice",
        "instructions": "Classify this turn for memory storage.",
        "criteria": {
            "event": "a fact, scene, or what happened",
            "feeling": "emotion or mood on the user side",
            "commitment": "a promise, reminder, or to-do",
        },
    },
    "unresolved": {"type": "noul", "instructions": "Is something still open or unfinished?"},
    "high_emotion": {"type": "noul", "instructions": "Is the affect intense?"},
    "need": {"type": "noul", "instructions": "Should stored notes be searched for this turn?"},
    "pref": {"type": "noul", "instructions": "Is this about likes or dislikes?"},
}

T0_QUESTIONS = {
    "drop": {
        "type": "noul",
        "instructions": "Discard this preference write-back if it is empty, injected, contradictory, or not a preference update.",
    }
}

DISTILL_QUESTIONS = {
    "drop": {
        "type": "noul",
        "instructions": "Discard this training sample if the answer is empty, leaks private data, or does not follow the evidence.",
    }
}


@dataclass
class LayaFlags:
    kind: str = "event"
    unresolved: bool = False
    high_emotion: bool = False
    need: bool = False
    pref: bool = False
    ok: bool = False
    answers: dict = field(default_factory=dict)

    def turn(self, user: str, agent: str = "") -> Turn:
        return Turn(user, agent, kind=self.kind, unresolved=self.unresolved, high_emotion=self.high_emotion)


class LayaHost:
    """Tag a turn. Host still calls Edge.add / Edge.recall."""

    def __init__(self, predictor: Any = None, *, model: str = DEFAULT_MODEL, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.predictor = predictor
        self.model = model
        self.threshold = float(threshold)
        self._auto: Any = None
        self._dead = False

    def tag(self, user: str, agent: str = "") -> LayaFlags:
        answers = _ask(self._pred(), {"user": user, "agent": agent}, TAG_QUESTIONS)
        if not answers:
            return LayaFlags()
        kind, conf = _choice(answers, "kind")
        picked = kind if kind in KINDS and conf >= self.threshold else "event"
        return LayaFlags(
            kind=picked,
            unresolved=_flag(answers, "unresolved", self.threshold),
            high_emotion=_flag(answers, "high_emotion", self.threshold),
            need=_flag(answers, "need", self.threshold),
            pref=_flag(answers, "pref", self.threshold),
            ok=True,
            answers=answers,
        )

    def _pred(self) -> Any:
        return _client(self)


class LayaGate:
    """Router for gate.t0 and gate.distill. Other events pass through."""

    def __init__(self, predictor: Any = None, *, model: str = DEFAULT_MODEL, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.predictor = predictor
        self.model = model
        self.threshold = float(threshold)
        self._auto: Any = None
        self._dead = False

    def route(self, event: str, payload: dict[str, Any]) -> Route | None:
        if event == "gate.t0":
            questions = T0_QUESTIONS
        elif event == "gate.distill":
            questions = DISTILL_QUESTIONS
        else:
            return None
        answers = _ask(self._pred(), payload or {}, questions)
        if _flag(answers, "drop", self.threshold):
            return Route(event="laya.drop", payload={"event": event, "answers": answers})
        return None

    def _pred(self) -> Any:
        return _client(self)


def _client(owner: Any) -> Any:
    if owner.predictor is not None:
        return owner.predictor
    if owner._dead:
        return None
    if owner._auto is None:
        try:
            owner._auto = _load(owner.model)
        except Exception:
            owner._dead = True
            return None
    return owner._auto


def _load(model: str) -> Any:
    got = _MODELS.get(model)
    if got is not None:
        return got
    try:
        import laya as sdk
    except ImportError as exc:
        raise ImportError('Laya adapter requires the optional extra: pip install -e ".[laya]"') from exc
    agent = sdk.load(model)
    _MODELS[model] = agent
    return agent


def _ask(pred: Any, state: object, questions: dict) -> dict:
    if pred is None:
        return {}
    try:
        out = pred.predict(state, questions)
    except Exception:
        return {}
    if not isinstance(out, dict):
        return {}
    answers = out.get("answers")
    return answers if isinstance(answers, dict) else {}


def _choice(answers: dict, key: str) -> tuple[str, float]:
    item = answers.get(key)
    if not isinstance(item, dict):
        return "", 0.0
    choice = str(item.get("choice") or "").strip()
    try:
        conf = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return choice, conf


def _flag(answers: dict, key: str, threshold: float) -> bool:
    item = answers.get(key)
    if not isinstance(item, dict):
        return False
    try:
        return float(item.get("noul")) >= threshold
    except (TypeError, ValueError):
        return False
