from __future__ import annotations

import json
import math
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from memco.bucket import Bucket
from memco.config import UNLIMITED
from memco.prefs import EMPTY_SELF, EMPTY_USER, bullets, has_pref, section

_BOUND = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]")

_SPLIT = re.compile(r"[\s,，。！？、；：:.!?()（）\[\]\"']+")
_STOP = set("the a an is to of and in on for it this that i you we they".split())


def _cache_payload(stamp: str, keyword: str, text: str) -> str:
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        parsed.setdefault("stamp", stamp)
        parsed.setdefault("keyword", keyword)
        if "body" not in parsed and "text" not in parsed:
            parsed["body"] = text
        return json.dumps(parsed, ensure_ascii=False)
    return json.dumps({"stamp": stamp, "keyword": keyword, "text": text, "body": text}, ensure_ascii=False)


class SimpleTok:
    def pick(self, text: str) -> str:
        parts = [p for p in _SPLIT.split(text or "") if p and p.lower() not in _STOP]
        if not parts:
            return (text or "").strip()[:8] or "scene"
        return max(parts, key=lambda w: (len(w), w.lower()))


class HashIndex:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.texts: list[str] = []
        self.ok = True

    def add(self, keyword: str, text: str) -> bool:
        self.keys.append(keyword)
        self.texts.append(text)
        return True

    def query(self, keyword: str, top_k: int = 3) -> tuple[list[str], bool]:
        kw = (keyword or "").strip()
        hits = [t for k, t in zip(self.keys, self.texts) if k == kw or bounded(kw, k) or bounded(kw, t)]
        if top_k != UNLIMITED and int(top_k) >= 0:
            hits = hits[: int(top_k)]
        return hits, False

    def rebuild(self, buckets: list[Bucket]) -> None:
        self.keys = []
        self.texts = []
        for bucket in buckets or []:
            if bucket.keyword or bucket.body:
                self.add(bucket.keyword, bucket.body)


def bounded(needle: str, hay: str) -> bool:
    if not needle or not hay or needle not in hay:
        return False
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return False
        left = hay[i - 1] if i > 0 else ""
        right = hay[i + len(needle)] if i + len(needle) < len(hay) else ""
        if (not left or not _BOUND.match(left)) and (not right or not _BOUND.match(right)):
            return True
        start = i + 1


@dataclass
class _Row:
    payload: str
    ts: float
    freq: int
    ts_key: str
    kw_key: str


class MemCache:
    def __init__(self, cap: int, ttl: int, fill: float, policy: str) -> None:
        self.cap = cap
        self.ttl = ttl
        self.fill = fill
        self.policy = policy
        self._items: OrderedDict[str, _Row] = OrderedDict()
        self._ts: dict[str, str] = {}
        self._kw: dict[str, str] = {}

    def _key(self, user_id: str, kind: str, value: str) -> str:
        return f"{user_id}:{kind}:{value}"

    def _iid(self, user_id: str, stamp: str, keyword: str) -> str:
        return f"{user_id}\0{stamp}\0{keyword}"

    def set_item(self, user_id: str, stamp: str, keyword: str, text: str) -> None:
        now = time.time()
        iid = self._iid(user_id, stamp, keyword)
        prev = self._items.get(iid)
        payload = _cache_payload(stamp, keyword, text)
        ts_key = self._key(user_id, "ts", stamp)
        kw_key = self._key(user_id, "kw", keyword)
        if prev is not None:
            if prev.ts_key != ts_key and self._ts.get(prev.ts_key) == iid:
                self._ts.pop(prev.ts_key, None)
            if prev.kw_key != kw_key and self._kw.get(prev.kw_key) == iid:
                self._kw.pop(prev.kw_key, None)
        self._items[iid] = _Row(payload, now, (prev.freq + 1) if prev else 1, ts_key, kw_key)
        self._ts[ts_key] = iid
        self._kw[kw_key] = iid
        if self.policy == "lru" or prev is None:
            self._items.move_to_end(iid)
        self._gc()

    def get_by_time(self, user_id: str, stamp: str) -> str | None:
        return self._touch(self._ts.get(self._key(user_id, "ts", stamp)))

    def get_by_keyword(self, user_id: str, keyword: str) -> str | None:
        return self._touch(self._kw.get(self._key(user_id, "kw", keyword)))

    def delete_item(self, user_id: str, stamp: str, keyword: str) -> None:
        self._drop(self._iid(user_id, stamp, keyword))

    def _touch(self, iid: str | None) -> str | None:
        if not iid:
            return None
        row = self._items.get(iid)
        if row is None:
            return None
        if self.ttl != UNLIMITED and time.time() - row.ts > self.ttl:
            self._drop(iid)
            return None
        row.freq += 1
        if self.policy == "lru":
            self._items.move_to_end(iid)
        return row.payload

    def _drop(self, iid: str) -> None:
        row = self._items.pop(iid, None)
        if row is None:
            return
        if self._ts.get(row.ts_key) == iid:
            self._ts.pop(row.ts_key, None)
            for other_id, other in self._items.items():
                if other.ts_key == row.ts_key:
                    self._ts[row.ts_key] = other_id
                    break
        if self._kw.get(row.kw_key) == iid:
            self._kw.pop(row.kw_key, None)
            for other_id, other in self._items.items():
                if other.kw_key == row.kw_key:
                    self._kw[row.kw_key] = other_id
                    break

    def _gc(self) -> None:
        now = time.time()
        if self.ttl != UNLIMITED:
            for iid, row in list(self._items.items()):
                if now - row.ts > self.ttl:
                    self._drop(iid)
        if self.cap == UNLIMITED or float(self.fill) < 0 or self.policy == "none":
            return
        mark = max(1, math.floor(self.cap * float(self.fill))) if self.fill > 0 else self.cap
        while len(self._items) > mark:
            self._evict_one()

    def _evict_one(self) -> None:
        if not self._items:
            return
        if self.policy == "random":
            self._drop(random.choice(list(self._items)))
        elif self.policy == "lfu":
            self._drop(min(self._items, key=lambda k: self._items[k].freq))
        else:
            self._drop(next(iter(self._items)))


class TemplateTeacher:
    """Built-in stand-in. Hosts should swap in a cloud model."""

    def complete(self, task: str, ctx: dict) -> dict:
        if task == "distill":
            q = str(ctx.get("question") or "").strip()
            ev = str(ctx.get("evidence") or q).strip()
            return {"answer": f"I remember: {ev.rstrip('.!?')}."}
        if task == "t0":
            source = str(ctx.get("source") or "")
            self_md = str(ctx.get("self_md") or EMPTY_SELF)
            user_md = str(ctx.get("user_md") or EMPTY_USER)
            return _grow(self_md, user_md, source)
        return {}


def _hate(text: str) -> bool:
    return bool(re.search(r"\b(?:hate|dislike)\b", (text or "").lower()))


def _grow(self_md: str, user_md: str, source: str) -> dict:
    user_likes, user_hates, self_likes, self_hates = [], [], [], []
    for raw in source.splitlines():
        line = raw.strip()
        if line.startswith("- user:"):
            text = line[len("- user:") :].strip()
            if _hate(text):
                user_hates.append(text)
            elif has_pref(text):
                user_likes.append(text)
        elif line.startswith("- agent:"):
            text = line[len("- agent:") :].strip()
            if _hate(text):
                self_hates.append(text)
            elif has_pref(text):
                self_likes.append(text)
    return {
        "self_md": _merge(self_md, self_likes, self_hates, self_kind=True),
        "user_md": _merge(user_md, user_likes, user_hates, self_kind=False),
    }


def _merge(md: str, likes: list[str], hates: list[str], self_kind: bool) -> str:
    base = EMPTY_SELF if self_kind else EMPTY_USER
    heading = "Self preference" if self_kind else "User preference"
    like_items = bullets(section(md, "likes")) + likes
    hate_items = bullets(section(md, "dislikes")) + hates

    def block(items: list[str]) -> str:
        uniq: list[str] = []
        seen: set[str] = set()
        for item in items:
            key = re.sub(r"\s+", "", item)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(item)
        if not uniq:
            return "(empty)\n"
        return "\n".join(f"- {x}" for x in uniq) + "\n"

    return (
        f"# {heading}\n\n## likes\n\n{block(like_items)}\n## dislikes\n\n{block(hate_items)}"
    )


class NoSnapshot:
    def load(self, path: str) -> None:
        return None


class NoBackup:
    def dump(self, root: Path) -> None:
        return None

    def restore(self, root: Path) -> None:
        return None
