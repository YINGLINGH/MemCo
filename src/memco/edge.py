from __future__ import annotations

import threading
import time
from pathlib import Path

from memco.config import MemoryConfig, at_cap
from memco.digest import Digest, Turn, digest
from memco.plugins import Bus, Snapshot, Tokenizer
from memco.prefs import EMPTY_SELF, EMPTY_USER, lookup
from memco.recall import Recall, is_empty, local_recall
from memco.routers import Route, Router, dispatch
from memco.short import forget_random
from memco.store import Store


class Edge:
    def __init__(
        self,
        cfg: MemoryConfig,
        root: Path,
        tok: Tokenizer,
        bus: Bus,
        snap: Snapshot | None = None,
        fail: Router | None = None,
        tok_fail: Router | None = None,
        end: Router | None = None,
    ) -> None:
        self.cfg = cfg
        self.tok = tok
        self.bus = bus
        self.snap = snap
        self.fail = fail
        self.tok_fail = tok_fail
        self.end = end
        self.store = Store(root / "short")
        self.self_path = root / "self.md"
        self.user_path = root / "user.md"
        if not self.self_path.exists():
            self.self_path.write_text(EMPTY_SELF, encoding="utf-8")
        if not self.user_path.exists():
            self.user_path.write_text(EMPTY_USER, encoding="utf-8")
        self.turns: list[Turn] = []
        self._idle_since: float | None = None
        self._wait = threading.Event()
        self._recall: dict = {}
        self._rid = 0
        bus.subscribe(cfg.topic("recall/res"), self._on_recall)
        bus.subscribe(cfg.topic("t0"), self._on_t0)
        bus.subscribe(cfg.topic("snap"), self._on_snap)

    def self_md(self) -> str:
        return self.self_path.read_text(encoding="utf-8")

    def user_md(self) -> str:
        return self.user_path.read_text(encoding="utf-8")

    def busy(self) -> None:
        self._idle_since = None

    def idle(self, now: float | None = None) -> None:
        if self.cfg.silence_sec <= 0:
            return
        self._idle_since = now if now is not None else time.time()

    def tick(self, now: float | None = None) -> bool:
        if self.cfg.silence_sec <= 0 or self._idle_since is None:
            return False
        t = now if now is not None else time.time()
        if t - self._idle_since < self.cfg.silence_sec:
            return False
        self.end_session()
        return True

    def add(self, turn: Turn) -> None:
        self.busy()
        self.turns.append(turn)

    def recall(self, text: str, *, need: bool, pref: bool, keyword: str, referred: str | None) -> tuple[Recall, Route | None]:
        t0 = lookup(self.self_md(), self.user_md(), text) if pref else []
        result = Recall(source="t0" if t0 else "none", hits=list(t0) if t0 and not need else [])
        if t0 and not need:
            result = Recall(hits=t0, source="t0")
        if need:
            try:
                kw = keyword or self.tok.pick(text)
            except Exception:
                kw = keyword or text[:8]
                dispatch(self.tok_fail, "tok.down", {"text": text})
            local = local_recall(self.store, kw, referred, self.cfg.recall_limit)
            if local:
                result = Recall(hits=[b.line() for b in local], source="short")
            elif not self.bus.connected():
                result = Recall(hits=t0, source="t0" if t0 else "net_down")
            else:
                deeper = self._ask_cloud(kw, referred)
                if deeper.hits:
                    result = deeper
                elif t0:
                    result = Recall(hits=t0, source="t0")
                else:
                    result = deeper
        route = None
        if is_empty(need, result):
            route = dispatch(self.fail, "recall.empty", {"text": text, "source": result.source})
        return result, route

    def end_session(self) -> Digest | None:
        if self.cfg.silence_sec == 0:
            dispatch(self.end, "session.end", {"n": len(self.turns)})
        if not self.turns:
            return None
        pack = digest(self.turns, self.tok, prior=self.store.keywords())
        sha = self.store.write_source(pack.source)
        for bucket in pack.buckets:
            bucket.source_sha256 = sha
            self.store.write(bucket)
        self._enforce_short()
        self.turns.clear()
        self._idle_since = None
        return pack

    def _enforce_short(self) -> None:
        if not at_cap(len(self.store.list_live()), self.cfg.short_cap):
            return
        if self.bus.connected() and self._upload():
            return
        cap = int(self.cfg.short_cap)
        while len(self.store.list_live()) > cap:
            if not forget_random(self.store):
                break
        self.store.gc_sources()

    def _upload(self) -> bool:
        live = list(self.store.list_live())
        if not live:
            return True
        texts: list[str] = []
        seen: set[str] = set()
        for bucket in live:
            sha = bucket.source_sha256
            if not sha or sha in seen:
                continue
            path = self.store.sources / f"{sha}.md"
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8"))
                seen.add(sha)
        ok = self.bus.publish(
            self.cfg.topic("archive"),
            {
                "stamp": live[-1].stamp,
                "keyword": live[0].keyword,
                "source": "\n\n".join(texts),
                "buckets": [b.to_dict() for b in live],
                "self_md": self.self_md(),
                "user_md": self.user_md(),
            },
        )
        if not ok:
            return False
        for bucket in live:
            self.store.delete(bucket.id)
        self.store.gc_sources()
        return True

    def _ask_cloud(self, keyword: str, referred: str | None) -> Recall:
        self._rid += 1
        rid = self._rid
        self._wait.clear()
        self._recall = {}
        if not self.bus.publish(
            self.cfg.topic("recall/req"),
            {"keyword": keyword, "referred": referred, "rid": rid},
        ):
            return Recall(source="net_down")
        timeout = None if self.cfg.net_timeout == -1 else float(self.cfg.net_timeout)
        if timeout is None:
            self._wait.wait()
        else:
            self._wait.wait(timeout=timeout)
        if self._recall.get("rid") not in (None, rid):
            return Recall(source="miss")
        hits = list(self._recall.get("hits") or [])
        return Recall(hits=hits, source=str(self._recall.get("source") or "miss"), vec_down=bool(self._recall.get("vec_down")))

    def _on_recall(self, payload: dict) -> None:
        if payload.get("rid") not in (None, self._rid):
            return
        self._recall = payload
        self._wait.set()

    def _on_t0(self, payload: dict) -> None:
        if payload.get("self_md") is not None:
            self.self_path.write_text(str(payload["self_md"]).rstrip() + "\n", encoding="utf-8")
        if payload.get("user_md") is not None:
            self.user_path.write_text(str(payload["user_md"]).rstrip() + "\n", encoding="utf-8")

    def _on_snap(self, payload: dict) -> None:
        path = str(payload.get("path") or "")
        if self.snap and path:
            self.snap.load(path)
