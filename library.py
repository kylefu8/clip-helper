"""Local directory inventory and durable per-file trim marks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".ts", ".webm", ".wmv"}
META_FIELDS = ("path", "name", "size_bytes", "mtime_ns", "duration_ms", "width", "height",
               "frame_rate", "codec", "rotation", "streams", "probe", "birthtime_ns", "file_id", "device_id",
               "atime_ns", "ctime_ns")
META_FIELDS += ("content_sha256",)


def content_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def same_source(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("size_bytes", "mtime_ns")) and all(
        left.get(key) is None or right.get(key) is None or str(left[key]) == str(right[key])
        for key in ("file_id", "birthtime_ns"))


def normalize_ranges(ranges, duration_ms: int) -> list[dict]:
    """Order and merge overlapping ranges; reject damaged or out-of-source marks."""
    ordered = []
    for value in ranges:
        start, end = int(value["start_ms"]), int(value["end_ms"])
        if not 0 <= start < end <= duration_ms:
            raise ValueError("保留区间必须在视频时长内，且结束晚于开始。")
        ordered.append({"start_ms": start, "end_ms": end})
    ordered.sort(key=lambda r: r["start_ms"])
    merged = []
    for interval in ordered:
        if merged and interval["start_ms"] <= merged[-1]["end_ms"]:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], interval["end_ms"])
        else:
            merged.append(interval.copy())
    return merged


def path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def default_data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "片段助手"


class StateStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "marks.json"
        self.lock = threading.RLock()
        self.warning = ""
        self.data = {"schema_version": 1, "last_directory": "", "last_selection": "", "files": {},
                     "excluded_files": {}}
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                if existing.get("schema_version") != 1 or not isinstance(existing.get("files"), dict):
                    raise ValueError("Unsupported marks format")
                if not isinstance(existing.get("excluded_files", {}), dict):
                    raise ValueError("Unsupported excluded files format")
                self.data.update(existing)
            except (OSError, ValueError, TypeError):
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
                backup = self.directory / f"marks-unreadable-{stamp}.json"
                shutil.copy2(self.path, backup)
                self.warning = f"上次的标记记录无法读取，已保留在 {backup.name}。可以继续使用。"

    def _save(self):
        temporary = self.path.with_suffix(".json.writing")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @property
    def last_directory(self) -> str:
        with self.lock:
            return self.data["last_directory"]

    @property
    def last_selection(self) -> str:
        with self.lock:
            return self.data["last_selection"]

    def set_directory(self, directory: str | Path):
        with self.lock:
            self.data["last_directory"] = str(Path(directory).resolve())
            self._save()

    def set_selection(self, path: str):
        with self.lock:
            self.data["last_selection"] = path
            self._save()

    def excluded_paths(self, directory: str | Path) -> set[str]:
        """Return the normalized paths excluded from one directory's visible list."""
        key = path_key(directory)
        with self.lock:
            paths = self.data.get("excluded_files", {}).get(key, [])
            return {str(path) for path in paths} if isinstance(paths, list) else set()

    def removed_count(self, directory: str | Path) -> int:
        return len(self.excluded_paths(directory))

    def is_excluded(self, directory: str | Path, file_path: str | Path) -> bool:
        return path_key(file_path) in self.excluded_paths(directory)

    def exclude_paths(self, directory: str | Path, paths) -> int:
        """Persist exclusions for direct child files, without touching file records."""
        root = Path(directory).expanduser().resolve()
        root_key = path_key(root)
        additions = set()
        for raw_path in paths:
            try:
                file_path = Path(raw_path).expanduser().resolve()
            except (OSError, TypeError, ValueError):
                continue
            if path_key(file_path.parent) == root_key:
                additions.add(path_key(file_path))
        if not additions:
            return 0
        with self.lock:
            excluded = self.data.setdefault("excluded_files", {})
            existing = set(excluded.get(root_key, []))
            added = additions - existing
            if added:
                excluded[root_key] = sorted(existing | additions, key=str.casefold)
                self._save()
            return len(added)

    def clear_exclusions(self, directory: str | Path) -> int:
        """Clear only the exclusion list belonging to the specified directory."""
        key = path_key(directory)
        with self.lock:
            excluded = self.data.setdefault("excluded_files", {})
            removed = excluded.pop(key, [])
            if removed:
                self._save()
            return len(removed) if isinstance(removed, list) else 0

    def cached_metadata(self, path: str | Path) -> dict | None:
        with self.lock:
            saved = copy.deepcopy(self.data["files"].get(path_key(path)))
        if not saved:
            return None
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        metadata = saved.get("metadata", {})
        if (metadata.get("size_bytes"), metadata.get("mtime_ns")) != (stat.st_size, stat.st_mtime_ns):
            return None
        if metadata.get("file_id") and str(stat.st_ino) != str(metadata["file_id"]):
            return None
        if metadata.get("content_sha256") and content_hash(path) != metadata["content_sha256"]:
            return None
        metadata["_content_hash_verified"] = bool(metadata.get("content_sha256"))
        return metadata if metadata.get("duration_ms", 0) > 0 else None

    def attach_saved(self, metadata: dict) -> dict:
        item = copy.deepcopy(metadata)
        verified_hash = item.pop("_content_hash_verified", False)
        item.update({"start_ms": None, "end_ms": None, "ranges": [], "draft_start_ms": None,
                     "draft_end_ms": None, "state": "unmarked", "result": None})
        with self.lock:
            saved = copy.deepcopy(self.data["files"].get(path_key(item["path"])))
        if not saved:
            return item
        previous = saved.get("metadata", {})
        if not same_source(previous, item):
            return item
        if not previous.get("content_sha256"):
            return item
        if previous.get("content_sha256"):
            current_hash = item.get("content_sha256") if verified_hash else content_hash(item["path"])
            if current_hash != previous["content_sha256"]:
                return item
            item["content_sha256"] = current_hash
        start = saved.get("draft_start_ms", saved.get("start_ms"))
        end = saved.get("draft_end_ms", saved.get("end_ms"))
        duration = item["duration_ms"]
        if start is not None and not 0 <= start < duration:
            start = None
        if end is not None and not 0 < end <= duration:
            end = None
        if start is not None and end is not None and start >= end:
            start, end = None, None
        ranges = saved.get("ranges")
        if ranges is None:
            ranges = [{"start_ms": start, "end_ms": end}] if start is not None and end is not None else []
        try:
            ranges = normalize_ranges(ranges, duration)
        except (ValueError, TypeError, KeyError):
            return item
        item.update({"start_ms": start, "end_ms": end, "draft_start_ms": start, "draft_end_ms": end,
                     "ranges": ranges, "state": saved.get("state", "unmarked"),
                     "result": saved.get("result"), "error": saved.get("error", "")})
        if saved.get("last_receipt"):
            item["last_receipt"] = saved["last_receipt"]
        result = item.get("result")
        if item["state"] == "prepared" and (not result or not Path(result.get("candidate_path", "")).is_file()):
            item.update({"state": "marked", "result": None})
        if item["state"] in {"preparing", "committing"}:
            item["state"] = "marked" if ranges else "unmarked"
        return item

    def save_item(self, item: dict):
        with self.lock:
            metadata = {field: copy.deepcopy(item[field]) for field in META_FIELDS if field in item}
            previous = self.data["files"].get(path_key(item["path"]), {})
            previous_metadata = previous.get("metadata", {})
            if same_source(previous_metadata, metadata) and previous_metadata.get("content_sha256"):
                metadata["content_sha256"] = previous_metadata["content_sha256"]
            record = {"metadata": metadata, "start_ms": item.get("start_ms"), "end_ms": item.get("end_ms"),
                      "state": item.get("state", "unmarked"), "result": copy.deepcopy(item.get("result")),
                      "error": item.get("error", "")}
            record["fingerprint_pending"] = bool(item.get("fingerprint_pending") and not metadata.get("content_sha256"))
            if item.get("last_receipt"):
                record["last_receipt"] = copy.deepcopy(item["last_receipt"])
            if "ranges" in item:
                record.update(ranges=copy.deepcopy(item["ranges"]),
                              draft_start_ms=item.get("draft_start_ms", item.get("start_ms")),
                              draft_end_ms=item.get("draft_end_ms", item.get("end_ms")))
            self.data["files"][path_key(item["path"])] = record
            self._save()

    def record_fingerprint(self, path: str, snapshot: dict, digest: str):
        """Publish a background fingerprint without overwriting newer mark edits."""
        with self.lock:
            record = self.data["files"].get(path_key(path))
            if not record or not same_source(record.get("metadata", {}), snapshot):
                return False
            stat = Path(path).stat()
            if (stat.st_size, stat.st_mtime_ns) != (snapshot["size_bytes"], snapshot["mtime_ns"]):
                return False
            if snapshot.get("file_id") and str(stat.st_ino) != str(snapshot["file_id"]):
                return False
            record["metadata"]["content_sha256"] = digest
            record["fingerprint_pending"] = False
            self._save()
            return True


def scan_directory(directory: str | Path, tools: dict, store: StateStore,
                   progress=None, cancel_event: threading.Event | None = None) -> list[dict]:
    from media_engine import inspect_video

    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("请选择一个存在的视频目录。")
    paths = sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
                   key=lambda p: p.name.casefold())
    excluded = store.excluded_paths(directory)
    paths = [path for path in paths if path_key(path) not in excluded]
    items = []
    for index, path in enumerate(paths):
        if cancel_event and cancel_event.is_set():
            break
        if progress:
            progress(index, len(paths), f"读取视频 {index + 1}/{len(paths)} · {path.name}")
        try:
            metadata = store.cached_metadata(path) or inspect_video(path, tools=tools)
            metadata["path"] = str(path)
            item = store.attach_saved(metadata)
        except Exception as error:
            snapshot = path.stat()
            item = {"path": str(path), "name": path.name, "size_bytes": snapshot.st_size,
                    "mtime_ns": snapshot.st_mtime_ns, "duration_ms": 0, "width": 0, "height": 0,
                    "frame_rate": 0, "codec": "", "rotation": 0, "start_ms": None, "end_ms": None,
                    "state": "error", "result": None, "error": str(error)}
            item.update(ranges=[], draft_start_ms=None, draft_end_ms=None)
        items.append(item)
    return items
