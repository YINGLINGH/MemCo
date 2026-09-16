from __future__ import annotations

from pathlib import Path

from memco.bucket import Bucket, dumps, loads, sha256_text


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.live = self.root / "buckets"
        self.sources = self.root / "sources"
        self.live.mkdir(parents=True, exist_ok=True)
        self.sources.mkdir(parents=True, exist_ok=True)

    def write_source(self, text: str) -> str:
        digest = sha256_text(text or "")
        path = self.sources / f"{digest}.md"
        if not path.exists():
            path.write_text(text or "", encoding="utf-8")
        return digest

    def write(self, bucket: Bucket) -> Path:
        bucket.archived = False
        path = self.live / f"b_{bucket.id}.md"
        path.write_text(dumps(bucket), encoding="utf-8")
        bucket.path = path
        return path

    def delete(self, bucket_id: str) -> bool:
        path = self.live / f"b_{bucket_id}.md"
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list_live(self) -> list[Bucket]:
        out: list[Bucket] = []
        for path in sorted(self.live.glob("*.md")):
            out.append(loads(path.read_text(encoding="utf-8"), archived=False, path=path))
        return out

    def gc_sources(self) -> None:
        keep = {b.source_sha256 for b in self.list_live() if b.source_sha256}
        for path in self.sources.glob("*.md"):
            if path.stem not in keep:
                path.unlink()

    def keywords(self) -> set[str]:
        return {b.keyword for b in self.list_live() if b.keyword}
