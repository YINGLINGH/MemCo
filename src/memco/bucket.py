from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KINDS = ("event", "feeling", "commitment")
BODY_LIMIT = 120


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def clip(text: str, limit: int = BODY_LIMIT) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + "…"


def as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Bucket:
    id: str
    kind: str
    stamp: str
    keyword: str
    body: str
    unresolved: bool = False
    high_emotion: bool = False
    source_sha256: str = ""
    distill: bool = False
    weight: int = 1
    archived: bool = False
    path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        kind = (self.kind or "event").strip()
        self.kind = kind if kind in KINDS else "event"
        self.body = clip(self.body)
        self.keyword = (self.keyword or "").strip() or "scene"
        self.id = (self.id or new_id()).strip()
        self.weight = max(1, int(self.weight or 1))

    def line(self) -> str:
        tags: list[str] = []
        if self.kind == "feeling":
            tags.append("feeling")
        elif self.kind == "commitment":
            tags.append("promise")
        if self.unresolved:
            tags.append("open")
        if self.high_emotion:
            tags.append("intense")
        prefix = f"[{','.join(tags)}] " if tags else ""
        stamp = (self.stamp or "").strip()
        head = f"{stamp} " if stamp else ""
        return f"{prefix}{head}{self.body}".strip()

    def to_meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "stamp": self.stamp,
            "keyword": self.keyword,
            "unresolved": bool(self.unresolved),
            "high_emotion": bool(self.high_emotion),
            "source_sha256": self.source_sha256,
            "distill": bool(self.distill),
            "weight": int(self.weight),
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.to_meta()
        data["body"] = self.body
        return data

    @classmethod
    def from_dict(cls, raw: dict | None) -> Bucket:
        data = dict(raw or {})
        return cls(
            id=str(data.get("id") or new_id()),
            kind=str(data.get("kind") or "event"),
            stamp=str(data.get("stamp") or ""),
            keyword=str(data.get("keyword") or ""),
            body=str(data.get("body") or data.get("text") or ""),
            unresolved=as_bool(data.get("unresolved")),
            high_emotion=as_bool(data.get("high_emotion")),
            source_sha256=str(data.get("source_sha256") or ""),
            distill=as_bool(data.get("distill") or data.get("distill_eligible")),
            weight=int(data.get("weight") or data.get("activation_count") or 1),
            archived=as_bool(data.get("archived")),
        )


def dumps(bucket: Bucket) -> str:
    fm = yaml.safe_dump(bucket.to_meta(), allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n\n{bucket.body.rstrip()}\n"


def loads(text: str, archived: bool = False, path: Path | None = None) -> Bucket:
    blob = text or ""
    meta: dict = {}
    body = blob.strip()
    if blob.startswith("---"):
        end = blob.find("\n---", 3)
        if end > 0:
            raw_fm = blob[3:end].strip()
            loaded = yaml.safe_load(raw_fm) if raw_fm else {}
            if isinstance(loaded, dict):
                meta = loaded
            body = blob[end + 4 :].lstrip("\n").strip()
    if not meta.get("body"):
        meta["body"] = body
    bucket = Bucket.from_dict(meta)
    if not str(meta.get("body") or "").strip():
        bucket.body = clip(body)
    bucket.archived = archived
    bucket.path = path
    return bucket
