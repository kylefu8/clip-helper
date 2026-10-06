from __future__ import annotations

"""Local, lossless video range preparation and guarded commit operations."""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from vendor.gpac_mp4_restore import restore_gpac_metadata, restore_movie_header_fields

Progress = Callable[[str], None]

GPAC_EXTENSIONS = {".mp4", ".mov", ".m4v"}
FFMPEG_COPY_EXTENSIONS = {
    ".mkv", ".avi", ".mts", ".m2ts", ".ts", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".3gp", ".ogv",
}
VIDEO_METADATA_KEYS = (
    "codec_name", "profile", "level", "codec_tag_string", "width", "height",
    "coded_width", "coded_height", "pix_fmt", "bits_per_raw_sample",
    "r_frame_rate", "sample_aspect_ratio", "display_aspect_ratio", "color_range",
    "color_space", "color_transfer", "color_primaries", "chroma_location", "field_order",
    "extradata_size", "extradata_hash",
)
AUDIO_METADATA_KEYS = (
    "codec_name", "profile", "codec_tag_string", "sample_fmt", "sample_rate",
    "channels", "channel_layout", "bits_per_sample", "bits_per_raw_sample", "initial_padding",
    "extradata_size", "extradata_hash",
)
OTHER_METADATA_KEYS = ("codec_name", "codec_tag_string", "codec_type", "profile", "extradata_size", "extradata_hash")
PACKET_TOLERANCE_SECONDS = 0.050
AV_SYNC_TOLERANCE_SECONDS = 0.050
PACKET_CONTEXT_SECONDS = 8.0


class TrimCancelled(RuntimeError):
    """Raised when a caller cancels an in-flight prepare operation."""


class UnsupportedMediaError(RuntimeError):
    """Raised when a container cannot be handled without risking stream loss."""


def _notify(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TrimCancelled("用户已取消准备；原素材未修改。")


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _tool_path(tools: dict | None, name: str, required: bool = True) -> str | None:
    value = tools.get(name) if tools else None
    if value:
        text = str(value)
        if Path(text).is_file() or shutil.which(text):
            return text
        raise FileNotFoundError(f"找不到视频工具 {name}: {text}")
    found = shutil.which(name)
    if found:
        return found
    if required:
        raise FileNotFoundError(f"找不到视频工具 {name}。")
    return None


def _run_capture(command: list[str], cancel_event: threading.Event | None = None,
                 timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a small-output process without opening a console window."""
    _check_cancel(cancel_event)
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=_creationflags(),
    )
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                process.communicate()
                raise TrimCancelled("用户已取消准备；正在结束视频工具。")
            if timeout is not None and time.monotonic() - started > timeout:
                process.kill()
                stdout, stderr = process.communicate()
                raise TimeoutError(f"视频工具超时: {command[0]}\n{stderr[-2000:]}")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _tail(path: Path, limit: int = 3000) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _run_to_file(command: list[str], stdout_path: Path, log_path: Path,
                 cancel_event: threading.Event | None = None,
                 progress: Progress | None = None, progress_message: str = "",
                 cwd: Path | None = None) -> None:
    """Stream large probe output to a private temporary file and logs to disk."""
    _check_cancel(cancel_event)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _notify(progress, progress_message)
    with stdout_path.open("xb") as stdout, log_path.open("ab") as stderr:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            cwd=str(cwd) if cwd is not None else None, creationflags=_creationflags(),
        )
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                process.wait()
                raise TrimCancelled("用户已取消准备；正在结束视频工具。")
            time.sleep(0.15)
        return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"视频工具失败，退出码 {return_code}: {command[0]}\n{_tail(log_path)}")


def _probe(path: Path, ffprobe: str, cancel_event: threading.Event | None = None) -> dict[str, Any]:
    for options in ([], ["-analyzeduration", "20000000", "-probesize", "100M"]):
        command = [
            ffprobe, "-v", "error", *options, "-show_format", "-show_streams", "-show_data_hash", "sha256",
            "-of", "json", str(path),
        ]
        completed = _run_capture(command, cancel_event=cancel_event, timeout=90)
        if completed.returncode:
            raise RuntimeError(f"ffprobe 读取失败: {path}\n{completed.stderr[-3000:]}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"ffprobe 返回了无效 JSON: {path}") from error
        # A high-bitrate video can exhaust the default probe before AAC packets
        # identify their profile. Read more before comparing codec configuration.
        missing_profile = any(
            stream.get("codec_name") == "aac"
            and str(stream.get("profile") or "").casefold() in ("", "unknown")
            for stream in result.get("streams", [])
        )
        if not missing_profile:
            return result
    raise UnsupportedMediaError(f"无法完整读取 AAC 音频编码规格，已停止裁剪: {path.name}")


def _duration_seconds(probe: dict[str, Any]) -> float:
    values: list[float] = []
    raw = (probe.get("format") or {}).get("duration")
    try:
        if raw not in (None, "N/A", ""):
            values.append(float(raw))
    except (TypeError, ValueError):
        pass
    if values:
        return max(values)
    for stream in probe.get("streams") or []:
        try:
            if stream.get("duration") not in (None, "N/A", ""):
                values.append(float(stream["duration"]))
        except (TypeError, ValueError):
            continue
    return max(values, default=0.0)


def _first_video(probe: dict[str, Any]) -> dict[str, Any]:
    for stream in probe.get("streams") or []:
        disposition = stream.get("disposition") or {}
        if stream.get("codec_type") == "video" and not int(disposition.get("attached_pic") or 0):
            return stream
    raise ValueError("文件中没有可播放的视频轨。")


def _rotation(stream: dict[str, Any]) -> float | None:
    for side in stream.get("side_data_list") or []:
        if side.get("side_data_type") == "Display Matrix" and side.get("rotation") is not None:
            try:
                return float(side["rotation"])
            except (TypeError, ValueError):
                return None
    rotate = (stream.get("tags") or {}).get("rotate")
    try:
        return float(rotate) if rotate is not None else None
    except (TypeError, ValueError):
        return None


def _frame_rate(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        try:
            rate = Fraction(str(value))
            if rate > 0:
                return float(rate)
        except (ValueError, ZeroDivisionError, TypeError):
            continue
    return 0.0


def _birthtime_ns(stat: os.stat_result) -> int | None:
    value = getattr(stat, "st_birthtime_ns", None)
    if value is not None:
        return int(value)
    value = getattr(stat, "st_birthtime", None)
    return int(value * 1_000_000_000) if value is not None else None


def _stat_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns),
        "atime_ns": int(stat.st_atime_ns), "ctime_ns": int(stat.st_ctime_ns),
        "birthtime_ns": _birthtime_ns(stat), "file_id": str(getattr(stat, "st_ino", "")),
        "device_id": int(getattr(stat, "st_dev", 0)),
    }


def inspect_video(path: str | Path, tools: dict | None = None) -> dict[str, Any]:
    """Return playback metadata plus a filesystem snapshot for safe later use."""
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"找不到视频文件: {target}")
    ffprobe = _tool_path(tools, "ffprobe")
    assert ffprobe is not None
    before = _stat_info(target)
    media_probe = _probe(target, ffprobe)
    after = _stat_info(target)
    if (before["size_bytes"], before["mtime_ns"], before["file_id"]) != (
        after["size_bytes"], after["mtime_ns"], after["file_id"]
    ):
        raise RuntimeError(f"读取视频期间文件发生变化: {target}")
    stream = _first_video(media_probe)
    duration_ms = int(round(_duration_seconds(media_probe) * 1000))
    if duration_ms <= 0:
        raise ValueError(f"无法取得有效时长: {target}")
    return {
        "path": str(target), "name": target.name,
        "size_bytes": before["size_bytes"], "mtime_ns": before["mtime_ns"],
        "atime_ns": before["atime_ns"], "ctime_ns": before["ctime_ns"],
        "birthtime_ns": before["birthtime_ns"], "file_id": before["file_id"],
        "device_id": before["device_id"], "duration_ms": duration_ms,
        "width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0),
        "frame_rate": _frame_rate(stream), "codec": str(stream.get("codec_name") or ""),
        "rotation": _rotation(stream), "streams": media_probe.get("streams") or [],
        "probe": media_probe,
    }


def _snapshot_matches(source: dict[str, Any], path: Path) -> tuple[bool, dict[str, Any], list[str]]:
    current = _stat_info(path)
    errors: list[str] = []
    for field in ("size_bytes", "mtime_ns"):
        expected = source.get(field)
        if expected is None or int(expected) != int(current[field]):
            errors.append(f"source_{field}_changed")
    expected_birthtime = source.get("birthtime_ns")
    if expected_birthtime is not None and current["birthtime_ns"] is not None:
        if int(expected_birthtime) != int(current["birthtime_ns"]):
            errors.append("source_birthtime_changed")
    expected_file_id = source.get("file_id")
    if expected_file_id not in (None, "") and str(expected_file_id) != current["file_id"]:
        errors.append("source_file_identity_changed")
    return not errors, current, errors


def _normalise_ranges(ranges: list[dict[str, Any]], duration_ms: int) -> list[dict[str, int]]:
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("至少标记一个保留区间。")
    ordered: list[dict[str, int]] = []
    for value in ranges:
        try:
            start, end = int(value["start_ms"]), int(value["end_ms"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("保留区间必须包含整数 start_ms 和 end_ms。") from error
        if not 0 <= start < end <= duration_ms:
            raise ValueError("保留区间必须在视频时长内，且结束晚于开始。")
        ordered.append({"start_ms": start, "end_ms": end})
    ordered.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    merged: list[dict[str, int]] = []
    for interval in ordered:
        if merged and interval["start_ms"] <= merged[-1]["end_ms"]:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], interval["end_ms"])
        else:
            merged.append(interval.copy())
    return merged


def _compact_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in line.rstrip("\r\n").split("|"):
        key, marker, value = part.partition("=")
        if marker:
            fields[key] = value
    return fields


def _time_value(value: str | None) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def _source_keyframe(source_path: Path, source_probe: dict[str, Any], requested_start_s: float,
                      ffprobe: str, job_dir: Path, log_path: Path,
                      cancel_event: threading.Event | None, progress: Progress | None) -> float:
    if requested_start_s <= 0:
        return 0.0
    stream_index = int(_first_video(source_probe)["index"])
    packet_file = job_dir / "keyframe-scan.tmp"
    end = requested_start_s + 0.001
    command = [
        ffprobe, "-v", "error", "-select_streams", str(stream_index),
        "-read_intervals", f"0%{end:.6f}", "-show_packets",
        "-show_entries", "packet=pts_time,dts_time,flags", "-of", "compact=p=0:nk=0",
        str(source_path),
    ]
    _run_to_file(command, packet_file, log_path, cancel_event, progress, "定位前一个关键帧")
    keys: list[float] = []
    with packet_file.open("r", encoding="utf-8", errors="replace") as stream_file:
        for line in stream_file:
            fields = _compact_fields(line)
            if "K" not in fields.get("flags", ""):
                continue
            pts = _time_value(fields.get("pts_time"))
            if pts is None:
                pts = _time_value(fields.get("dts_time"))
            if pts is not None and pts <= requested_start_s + 0.000001:
                keys.append(pts)
    packet_file.unlink(missing_ok=True)
    if not keys:
        raise UnsupportedMediaError(f"在 {requested_start_s:.3f} 秒之前找不到可用关键帧。")
    return max(0.0, max(keys))


def _stream_signature(stream: dict[str, Any]) -> tuple[str, str, str, int]:
    disposition = stream.get("disposition") or {}
    return (str(stream.get("codec_type") or ""), str(stream.get("codec_name") or ""),
            str(stream.get("codec_tag_string") or ""), int(disposition.get("attached_pic") or 0))


def _pair_streams(source_probe: dict[str, Any], candidate_probe: dict[str, Any]) -> list[tuple[dict, dict]]:
    source_streams = list(source_probe.get("streams") or [])
    candidate_streams = list(candidate_probe.get("streams") or [])
    if Counter(map(_stream_signature, source_streams)) != Counter(map(_stream_signature, candidate_streams)):
        raise UnsupportedMediaError("裁剪结果的轨道数量或类型与原片不一致；未确认或替换原片。")
    candidates: dict[tuple[str, str, str, int], deque] = defaultdict(deque)
    for stream in sorted(candidate_streams, key=lambda item: int(item.get("index", 0))):
        candidates[_stream_signature(stream)].append(stream)
    pairs: list[tuple[dict, dict]] = []
    for stream in sorted(source_streams, key=lambda item: int(item.get("index", 0))):
        queue = candidates[_stream_signature(stream)]
        if not queue:
            raise UnsupportedMediaError("无法把裁剪后的轨道对应回原片。")
        pairs.append((stream, queue.popleft()))
    return pairs


def _format_tag_equal(left: Any, right: Any, key: str) -> bool:
    if key.casefold() != "creation_time":
        return left == right
    if left == right:
        return True
    try:
        def parse(value: str):
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return abs(parse(left) - parse(right)) <= 0.001
    except (ValueError, TypeError, OverflowError):
        return False


def _metadata_check(source_probe: dict[str, Any], candidate_probe: dict[str, Any],
                    pairs: list[tuple[dict, dict]], require_rotation: bool = True) -> dict[str, Any]:
    errors: list[str] = []
    track_checks: list[dict[str, Any]] = []
    matroska = "matroska" in str((source_probe.get("format") or {}).get("format_name", ""))
    for source_stream, candidate_stream in pairs:
        kind = str(source_stream.get("codec_type") or "")
        attached = int((source_stream.get("disposition") or {}).get("attached_pic") or 0)
        keys = VIDEO_METADATA_KEYS if kind == "video" and not attached else (
            AUDIO_METADATA_KEYS if kind == "audio" else OTHER_METADATA_KEYS
        )
        # r_frame_rate is inferred from timestamps and can change when a
        # discontinuous stream-copy edit is muxed. The codec/profile and packet
        # sequence checks below remain the acceptance criteria for frame data.
        derived_differences = {
            key: {"source": source_stream.get(key), "candidate": candidate_stream.get(key)}
            for key in keys if key == "r_frame_rate" and source_stream.get(key) != candidate_stream.get(key)
        }
        differences = {
            key: {"source": source_stream.get(key), "candidate": candidate_stream.get(key)}
            for key in keys if key != "r_frame_rate" and source_stream.get(key) != candidate_stream.get(key)
        }
        src_tags = source_stream.get("tags") or {}
        dst_tags = candidate_stream.get("tags") or {}
        # The Matroska muxer writes each track's measured duration as a tag;
        # a shorter edit must have a different value. Keep this visible in
        # the report while checking all descriptive tags as before.
        derived_tags = {key: {"source": value, "candidate": dst_tags.get(key)}
                        for key, value in src_tags.items()
                        if matroska and key.casefold() == "duration" and dst_tags.get(key) != value}
        ignored_tag_differences = {
            key: {"source": value, "candidate": dst_tags.get(key)}
            for key, value in src_tags.items()
            if key.casefold() == "encoder"
            and str(value).casefold().startswith(("lavc", "lavf", "ffmpeg", "gpac"))
            and dst_tags.get(key) != value
        }
        tag_differences = {key: {"source": value, "candidate": dst_tags.get(key)}
                           for key, value in src_tags.items()
                           if not (key.casefold() == "encoder" and str(value).casefold().startswith(("lavc", "lavf", "ffmpeg", "gpac")))
                           and not (matroska and key.casefold() == "duration")
                           and not _format_tag_equal(value, dst_tags.get(key), key)}
        src_rotation, dst_rotation = _rotation(source_stream), _rotation(candidate_stream)
        rotation_equal = src_rotation == dst_rotation or (
            src_rotation is not None and dst_rotation is not None and abs(src_rotation - dst_rotation) <= 0.001
        )
        src_matrix = next((item.get("displaymatrix") for item in source_stream.get("side_data_list") or []
                           if item.get("side_data_type") == "Display Matrix"), None)
        dst_matrix = next((item.get("displaymatrix") for item in candidate_stream.get("side_data_list") or []
                           if item.get("side_data_type") == "Display Matrix"), None)
        matrix_equal = src_matrix == dst_matrix
        stream_ok = not differences and not tag_differences and (not require_rotation or (rotation_equal and matrix_equal))
        track_checks.append({
            "source_index": int(source_stream.get("index", -1)),
            "candidate_index": int(candidate_stream.get("index", -1)),
            "codec_type": kind,
            "metadata_equal": stream_ok,
            "property_differences": differences,
            "derived_property_differences": derived_differences,
            "derived_tag_differences": derived_tags,
            "tag_differences": tag_differences,
            "ignored_encoder_tag_differences": ignored_tag_differences,
            "rotation_source": src_rotation,
            "rotation_candidate": dst_rotation,
            "rotation_equal": rotation_equal,
            "display_matrix_equal": matrix_equal,
        })
        if not stream_ok:
            errors.append(f"stream_{source_stream.get('index')}_metadata_changed")
    src_tags = (source_probe.get("format") or {}).get("tags") or {}
    dst_tags = (candidate_probe.get("format") or {}).get("tags") or {}
    tag_differences = {key: {"source": value, "candidate": dst_tags.get(key)}
                       for key, value in src_tags.items()
                       if not _format_tag_equal(value, dst_tags.get(key), key)}
    if tag_differences:
        errors.append("container_metadata_changed")
    source_format = source_probe.get("format") or {}
    candidate_format = candidate_probe.get("format") or {}
    format_name_source = str(source_format.get("format_name") or "")
    format_name_candidate = str(candidate_format.get("format_name") or "")
    if not format_name_source or not format_name_candidate:
        errors.append("container_format_missing")
    return {"passed": not errors, "errors": errors, "track_checks": track_checks,
            "container_tag_differences": tag_differences,
            "format_name_source": format_name_source, "format_name_candidate": format_name_candidate}


def _packet_hashes(path: Path, stream_index: int, ffprobe: str, job_dir: Path,
                   label: str, interval: tuple[float, float] | None,
                   cancel_event: threading.Event | None, progress: Progress | None) -> list[dict[str, Any]]:
    packet_file = job_dir / f"{label}-s{stream_index}-packets.tmp"
    log_path = job_dir / f"{label}-s{stream_index}-packets.log"
    command = [ffprobe, "-v", "error", "-select_streams", str(stream_index)]
    if interval is not None:
        command += ["-read_intervals", f"{interval[0]:.6f}%{interval[1]:.6f}"]
    command += ["-show_packets", "-show_data_hash", "sha256",
                "-show_entries", "packet=pts_time,dts_time,duration_time,data_hash,flags",
                "-of", "compact=p=0:nk=0", str(path)]
    _run_to_file(command, packet_file, log_path, cancel_event, progress, "逐轨比对媒体包")
    packets: list[dict[str, Any]] = []
    try:
        with packet_file.open("r", encoding="utf-8", errors="replace") as stream_file:
            for line in stream_file:
                fields = _compact_fields(line)
                digest = fields.get("data_hash")
                if digest and ":" in digest:
                    digest = digest.split(":", 1)[1]
                if not digest:
                    raise RuntimeError(f"ffprobe 未返回数据包 SHA-256: {path} stream {stream_index}")
                packets.append({"hash": digest.upper(), "pts": _time_value(fields.get("pts_time")),
                                "dts": _time_value(fields.get("dts_time")),
                                "duration": _time_value(fields.get("duration_time"))})
    finally:
        packet_file.unlink(missing_ok=True)
    return packets


def _kmp_starts(haystack: list[str], needle: list[str]) -> list[int]:
    if not needle or len(needle) > len(haystack):
        return []
    failure = [0] * len(needle)
    matched = 0
    for index in range(1, len(needle)):
        while matched and needle[index] != needle[matched]:
            matched = failure[matched - 1]
        if needle[index] == needle[matched]:
            matched += 1
            failure[index] = matched
    results: list[int] = []
    matched = 0
    for index, value in enumerate(haystack):
        while matched and value != needle[matched]:
            matched = failure[matched - 1]
        if value == needle[matched]:
            matched += 1
            if matched == len(needle):
                results.append(index - len(needle) + 1)
                matched = failure[matched - 1]
    return results


def _median(values: list[float]) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    middle = len(clean) // 2
    return clean[middle] if len(clean) % 2 else (clean[middle - 1] + clean[middle]) / 2


def _offset_values(source_packets: list[dict], candidate_packets: list[dict],
                   source_start: int, key: str, limit: int = 256) -> list[float]:
    offsets = []
    for index in range(min(limit, len(candidate_packets))):
        source_value = source_packets[source_start + index].get(key)
        candidate_value = candidate_packets[index].get(key)
        if source_value is not None and candidate_value is not None:
            offsets.append(float(source_value - candidate_value))
    return offsets


def _select_match(starts: list[int], source_packets: list[dict], candidate_packets: list[dict],
                  stream: dict[str, Any], segment_start_s: float,
                  primary_offset: float | None) -> tuple[int, float | None]:
    attached = int((stream.get("disposition") or {}).get("attached_pic") or 0) == 1
    if attached:
        return min(starts), None
    choices: list[tuple[float, int, float | None]] = []
    for start in starts:
        pts = source_packets[start].get("pts")
        pts_distance = abs(float(pts) - segment_start_s) if pts is not None else 1e9
        offsets = _offset_values(source_packets, candidate_packets, start, "pts")
        median_offset = _median(offsets)
        offset_distance = abs(median_offset - primary_offset) if median_offset is not None and primary_offset is not None else 0.0
        choices.append((pts_distance + offset_distance * 3.0, start, median_offset))
    _, start, offset = min(choices, key=lambda item: (item[0], item[1]))
    return start, offset


def _track_role(stream: dict[str, Any]) -> str:
    if int((stream.get("disposition") or {}).get("attached_pic") or 0):
        return "attached_picture"
    return str(stream.get("codec_type") or "unknown")


def _track_config_check(source_stream: dict[str, Any], candidate_stream: dict[str, Any]) -> list[str]:
    kind = str(source_stream.get("codec_type") or "")
    attached = int((source_stream.get("disposition") or {}).get("attached_pic") or 0)
    keys = VIDEO_METADATA_KEYS if kind == "video" and not attached else (
        AUDIO_METADATA_KEYS if kind == "audio" else OTHER_METADATA_KEYS
    )
    return [key for key in keys if source_stream.get(key) != candidate_stream.get(key)]


def _verify_split_segment(source_path: Path, source_probe: dict[str, Any], segment_path: Path,
                          segment_index: int, source_range: dict[str, Any], ffprobe: str,
                          job_dir: Path, cancel_event: threading.Event | None,
                          progress: Progress | None) -> dict[str, Any]:
    candidate_probe = _probe(segment_path, ffprobe, cancel_event)
    pairs = _pair_streams(source_probe, candidate_probe)
    main_source = _first_video(source_probe)
    duration = _duration_seconds(source_probe)
    scan_start = max(0.0, float(source_range["actual_start_s"]) - PACKET_CONTEXT_SECONDS)
    scan_end = min(duration + 0.1, float(source_range["requested_end_s"]) + PACKET_CONTEXT_SECONDS)
    source_interval = (scan_start, max(scan_start + 0.001, scan_end))
    proofs: dict[str, Any] = {}
    primary_offset: float | None = None
    for source_stream, candidate_stream in pairs:
        _check_cancel(cancel_event)
        changed = _track_config_check(source_stream, candidate_stream)
        if changed:
            raise UnsupportedMediaError(
                f"第 {segment_index} 段 stream {source_stream['index']} 码流配置变化: {', '.join(changed)}"
            )
        attached = int((source_stream.get("disposition") or {}).get("attached_pic") or 0) == 1
        interval = None if attached else source_interval
        src_packets = _packet_hashes(source_path, int(source_stream["index"]), ffprobe, job_dir,
                                     f"srcseg{segment_index:02d}", interval, cancel_event, progress)
        dst_packets = _packet_hashes(segment_path, int(candidate_stream["index"]), ffprobe, job_dir,
                                     f"candseg{segment_index:02d}", None, cancel_event, progress)
        source_start_index: int | None = None
        if dst_packets:
            if not src_packets:
                raise UnsupportedMediaError(f"第 {segment_index} 段 stream {source_stream['index']} 找不到原片对应数据包。")
            starts = _kmp_starts([packet["hash"] for packet in src_packets],
                                 [packet["hash"] for packet in dst_packets])
            if not starts:
                raise UnsupportedMediaError(
                    f"第 {segment_index} 段 stream {source_stream['index']} 不是原片中的连续包序列。"
                )
            source_start_index, offset = _select_match(
                starts, src_packets, dst_packets, source_stream,
                float(source_range["actual_start_s"]), primary_offset,
            )
            if source_stream is main_source:
                primary_offset = offset
        elif source_stream.get("codec_type") in ("video", "audio"):
            raise UnsupportedMediaError(f"第 {segment_index} 段缺少视频或音频数据包。")

        matched_source = (src_packets[source_start_index:source_start_index + len(dst_packets)]
                          if source_start_index is not None else [])
        if len(matched_source) != len(dst_packets):
            raise RuntimeError("packet matcher returned a truncated stream mapping")
        pts_offsets = _offset_values(matched_source, dst_packets, 0, "pts", limit=len(dst_packets))
        dts_offsets = _offset_values(matched_source, dst_packets, 0, "dts", limit=len(dst_packets))
        pts_median = _median(pts_offsets)
        pts_span = max(pts_offsets) - min(pts_offsets) if pts_offsets else None
        dts_span = max(dts_offsets) - min(dts_offsets) if dts_offsets else None
        if source_stream.get("codec_type") in ("video", "audio") and not attached and pts_median is None:
            raise UnsupportedMediaError(f"stream {source_stream['index']} 缺少时间戳，无法验证音画同步。")
        if pts_span is not None and pts_span > PACKET_TOLERANCE_SECONDS:
            raise UnsupportedMediaError(f"第 {segment_index} 段 stream {source_stream['index']} PTS 发生漂移。")
        if dts_span is not None and dts_span > PACKET_TOLERANCE_SECONDS:
            raise UnsupportedMediaError(f"第 {segment_index} 段 stream {source_stream['index']} DTS 发生漂移。")
        proofs[str(source_stream["index"])] = {
            "source_stream": source_stream, "candidate_stream": candidate_stream,
            "hashes": [packet["hash"] for packet in dst_packets],
            "source_pts": [packet["pts"] for packet in matched_source],
            "source_dts": [packet["dts"] for packet in matched_source],
            "source_durations": [packet["duration"] for packet in matched_source],
            "candidate_pts": [packet["pts"] for packet in dst_packets],
            "candidate_dts": [packet["dts"] for packet in dst_packets],
            "pts_offset_seconds": pts_median, "pts_offset_span_seconds": pts_span,
            "dts_offset_span_seconds": dts_span, "source_packet_count": len(dst_packets),
            "matched_source_start_index": source_start_index,
            "payload_sequence_matches": True, "role": _track_role(source_stream),
        }
    if primary_offset is None:
        raise UnsupportedMediaError(f"第 {segment_index} 段主视频无法建立时间戳对应关系。")
    return {"probe": candidate_probe, "pairs": pairs, "proofs": proofs,
            "primary_offset_seconds": primary_offset}


def _actual_range_from_proof(segment: dict[str, Any], source_range: dict[str, Any], index: int) -> dict[str, Any]:
    main_proof = next(
        proof for proof in segment["proofs"].values()
        if proof["source_stream"].get("codec_type") == "video"
        and not int((proof["source_stream"].get("disposition") or {}).get("attached_pic") or 0)
    )
    pts = [float(value) for value in main_proof["source_pts"] if value is not None]
    durations = [float(value) if value is not None else 0.0 for value in main_proof["source_durations"]]
    actual_start = min(pts) if pts else float(source_range["actual_start_s"])
    actual_end = max((p + d for p, d in zip(pts, durations)), default=max(pts, default=actual_start))
    return {
        "index": index,
        "requested_start_ms": int(source_range["requested_start_ms"]),
        "requested_end_ms": int(source_range["requested_end_ms"]),
        "actual_start_ms": max(0, int(round(actual_start * 1000))),
        "actual_end_ms": max(0, int(round(actual_end * 1000))),
        "actual_start_s": actual_start, "actual_end_s": actual_end,
        "source_packet_count": main_proof["source_packet_count"],
        "merged_requested_ranges": source_range["merged_requested_ranges"],
    }


def _gpac_strip_attached_tracks(segment_path: Path, source_probe: dict[str, Any],
                                mp4box: str, job_dir: Path, index: int,
                                cancel_event: threading.Event | None,
                                progress: Progress | None) -> Path | None:
    attached_ids = []
    for stream in source_probe.get("streams") or []:
        if not int((stream.get("disposition") or {}).get("attached_pic") or 0):
            continue
        try:
            track_id = int(str(stream.get("id")), 0)
            if track_id > 0:
                attached_ids.append(track_id)
        except (TypeError, ValueError) as error:
            raise UnsupportedMediaError("GPAC 无法安全识别封面轨 ID，不能拼接多区间。") from error
    if not attached_ids:
        return None
    output_path = job_dir / f"segment-{index:02d}-without-cover{segment_path.suffix}"
    command = [mp4box, "-p=0"]
    for track_id in attached_ids:
        command += ["-rem", str(track_id)]
    command += ["-out", str(output_path), str(segment_path)]
    stdout = job_dir / f"mp4box-strip-cover-{index:02d}.stdout.tmp"
    _run_to_file(command, stdout, job_dir / f"mp4box-strip-cover-{index:02d}.log",
                 cancel_event, progress, f"临时移除第 {index} 段重复封面轨")
    stdout.unlink(missing_ok=True)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"MP4Box 没有生成第 {index} 段的无封面临时片段。")
    return output_path


def _gpac_concat(segment_paths: list[Path], output_path: Path, mp4box: str,
                 job_dir: Path, cancel_event: threading.Event | None,
                 progress: Progress | None) -> None:
    if len(segment_paths) == 1:
        shutil.copyfile(segment_paths[0], output_path)
        return
    command = [mp4box, "-p=0", "-add", str(segment_paths[0])]
    for path in segment_paths[1:]:
        if any(char in path.name for char in ("+", "#")):
            raise UnsupportedMediaError("暂存文件名包含 GPAC 拼接符号 + 或 #，不能安全拼接。")
        # Cat a whole segment at once so GPAC keeps its audio, video and data
        # tracks aligned on the same timeline. The following segment has had
        # only its duplicate attached-picture track removed.
        command += ["-cat", path.name]
    command += ["-new", str(output_path)]
    stdout = job_dir / "mp4box-concat.stdout.tmp"
    _run_to_file(command, stdout, job_dir / "mp4box-concat.log",
                 cancel_event, progress, "GPAC 无损拼接保留区间", cwd=job_dir)
    stdout.unlink(missing_ok=True)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("MP4Box 没有生成拼接候选文件。")


def _ffmpeg_segment(source_path: Path, output_path: Path, actual_start_s: float,
                    requested_end_s: float, ffmpeg: str, job_dir: Path, index: int,
                    cancel_event: threading.Event | None, progress: Progress | None) -> None:
    duration = requested_end_s - actual_start_s
    if duration <= 0:
        raise ValueError("关键帧对齐后保留区间长度无效。")
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "warning",
        # Keep source timestamps through seeking, then shift all tracks together.
        # Matroska can seek to an earlier cluster; subtracting the requested
        # start first would create negative PTS and lose their original spacing.
        "-copyts", "-ss", f"{actual_start_s:.6f}", "-i", str(source_path), "-to", f"{requested_end_s:.6f}",
        "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy", "-copy_unknown",
        "-avoid_negative_ts", "make_zero", str(output_path),
    ]
    stdout = job_dir / f"ffmpeg-segment-{index:02d}.stdout.tmp"
    _run_to_file(command, stdout, job_dir / f"ffmpeg-segment-{index:02d}.log",
                 cancel_event, progress, f"无损裁剪第 {index} 段")
    stdout.unlink(missing_ok=True)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg 没有生成第 {index} 段候选文件。")


def _ffmpeg_concat(segment_paths: list[Path], output_path: Path, ffmpeg: str,
                   job_dir: Path, cancel_event: threading.Event | None,
                   progress: Progress | None, source_path: Path,
                   source_probe: dict[str, Any]) -> None:
    if len(segment_paths) == 1:
        shutil.copyfile(segment_paths[0], output_path)
        return
    list_path = job_dir / "concat-inputs.txt"
    with list_path.open("x", encoding="utf-8", newline="\n") as stream:
        for path in segment_paths:
            unix_path = str(path.resolve()).replace("\\", "/")
            escaped = unix_path.replace("'", "'\\''")
            stream.write(f"file '{escaped}'\n")
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "warning", "-f", "concat", "-safe", "0",
        "-auto_convert", "0",
        "-i", str(list_path), "-i", str(source_path), "-map", "0", "-map_metadata", "1", "-map_chapters", "0",
    ]
    for stream in source_probe.get("streams") or []:
        index = int(stream["index"])
        command += [f"-map_metadata:s:{index}", f"1:s:{index}"]
    command += ["-c", "copy", "-copy_unknown", "-avoid_negative_ts", "disabled", str(output_path)]
    stdout = job_dir / "ffmpeg-concat.stdout.tmp"
    _run_to_file(command, stdout, job_dir / "ffmpeg-concat.log", cancel_event,
                 progress, "无损拼接保留区间")
    stdout.unlink(missing_ok=True)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg 没有生成拼接候选文件。")


def _gpac_split(source_path: Path, raw_output: Path, start_s: float, end_s: float,
                mp4box: str, job_dir: Path, index: int,
                cancel_event: threading.Event | None, progress: Progress | None) -> None:
    command = [mp4box, "-p=0", "-splitx", f"{start_s:.6f}:{end_s:.6f}",
               "-out", str(raw_output), str(source_path)]
    stdout = job_dir / f"mp4box-split-{index:02d}.stdout.tmp"
    _run_to_file(command, stdout, job_dir / f"mp4box-split-{index:02d}.log",
                 cancel_event, progress, f"GPAC 无损裁剪第 {index} 段")
    stdout.unlink(missing_ok=True)
    if not raw_output.is_file() or raw_output.stat().st_size == 0:
        raise RuntimeError(f"MP4Box 没有生成预期文件 {raw_output.name}。")


def _ffmpeg_decode(candidate: Path, stream: dict[str, Any], ffmpeg: str,
                   job_dir: Path, cancel_event: threading.Event | None,
                   progress: Progress | None) -> dict[str, Any]:
    kind = stream.get("codec_type")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode"]
    if kind == "video":
        command += ["-noautorotate"]
    command += ["-i", str(candidate), "-map", f"0:{stream['index']}"]
    if kind == "video":
        command += ["-an", "-sn", "-dn"]
    elif kind == "audio":
        command += ["-vn", "-sn", "-dn"]
    command += ["-f", "null", "NUL" if os.name == "nt" else "-"]
    stdout = job_dir / f"decode-stream-{stream['index']}.stdout.tmp"
    _run_to_file(command, stdout, job_dir / f"decode-stream-{stream['index']}.log",
                 cancel_event, progress, f"完整解码 {kind} 轨 {stream['index']}")
    stdout.unlink(missing_ok=True)
    return {"stream_index": int(stream["index"]), "codec_type": kind, "passed": True}


def _verify_final_candidate(source_path: Path, source_probe: dict[str, Any], candidate_path: Path,
                            segment_proofs: list[dict[str, Any]], final_probe: dict[str, Any],
                            ffprobe: str, ffmpeg: str, job_dir: Path,
                            cancel_event: threading.Event | None,
                            progress: Progress | None) -> dict[str, Any]:
    pairs = _pair_streams(source_probe, final_probe)
    metadata = _metadata_check(source_probe, final_probe, pairs, require_rotation=True)
    if not metadata["passed"]:
        details = [{"stream": row["source_index"], "properties": row["property_differences"],
                    "tags": row["tag_differences"], "rotation": row["rotation_equal"],
                    "matrix": row["display_matrix_equal"]}
                   for row in metadata["track_checks"] if not row["metadata_equal"]]
        raise UnsupportedMediaError(
            f"裁剪候选的码流规格、标签或旋转矩阵发生变化，已拒绝提交。"
            f" errors={metadata['errors']}; tracks={json.dumps(details, ensure_ascii=False)}; "
            f"container_tags={json.dumps(metadata['container_tag_differences'], ensure_ascii=False)}"
        )

    packet_checks: list[dict[str, Any]] = []
    segment_offsets: list[dict[str, float]] = [dict() for _ in segment_proofs]
    for source_stream, candidate_stream in pairs:
        _check_cancel(cancel_event)
        source_key = str(source_stream["index"])
        all_candidate = _packet_hashes(candidate_path, int(candidate_stream["index"]), ffprobe,
                                       job_dir, "final", None, cancel_event, progress)
        expected_hashes: list[str] = []
        expected_counts: list[int] = []
        per_segment: list[dict[str, Any]] = []
        attached = int((source_stream.get("disposition") or {}).get("attached_pic") or 0) == 1
        for segment_index, proof in enumerate(segment_proofs):
            one = proof["proofs"][source_key]
            hashes = [] if attached and segment_index > 0 else one["hashes"]
            expected_hashes.extend(hashes)
            expected_counts.append(len(hashes))
            per_segment.append(one)
        actual_hashes = [packet["hash"] for packet in all_candidate]
        if expected_hashes != actual_hashes:
            raise UnsupportedMediaError(
                f"最终候选 stream {source_stream['index']} 的数据包序列与分段原片序列不一致。"
            )

        offset_rows = []
        cursor = 0
        for segment_index, (count, proof) in enumerate(zip(expected_counts, per_segment)):
            chunk = all_candidate[cursor : cursor + count]
            cursor += count
            if not count:
                continue
            source_pts, source_dts = proof["source_pts"], proof["source_dts"]
            pts_offsets = [float(src - dst["pts"]) for src, dst in zip(source_pts, chunk)
                           if src is not None and dst.get("pts") is not None]
            # Matroska stores presentation timestamps. FFprobe infers DTS
            # using previous packets, so at a join the first reordered frames
            # inherit the preceding segment's timestamps. Verify all payloads
            # and PTS, and compare inferred DTS after this bounded prefix.
            inferred_prefix = 0
            if (segment_index > 0 and source_stream.get("codec_type") == "video"
                    and "matroska" in str((source_probe.get("format") or {}).get("format_name", ""))):
                inferred_prefix = max(0, int(source_stream.get("has_b_frames") or 0))
            dts_offsets = [float(src - dst["dts"]) for src, dst in zip(source_dts[inferred_prefix:], chunk[inferred_prefix:])
                           if src is not None and dst.get("dts") is not None]
            pts_median = _median(pts_offsets)
            pts_span = max(pts_offsets) - min(pts_offsets) if pts_offsets else None
            dts_span = max(dts_offsets) - min(dts_offsets) if dts_offsets else None
            if source_stream.get("codec_type") in ("video", "audio") and not attached and pts_median is None:
                raise UnsupportedMediaError(f"最终候选 stream {source_stream['index']} 缺少音画时间戳。")
            if pts_span is not None and pts_span > PACKET_TOLERANCE_SECONDS:
                raise UnsupportedMediaError(f"拼接后第 {segment_index + 1} 段 PTS 发生漂移。")
            if dts_span is not None and dts_span > PACKET_TOLERANCE_SECONDS:
                raise UnsupportedMediaError(f"拼接后第 {segment_index + 1} 段 DTS 发生漂移。")
            if source_stream.get("codec_type") in ("video", "audio") and not attached and pts_median is not None:
                segment_offsets[segment_index][source_key] = pts_median
            offset_rows.append({"segment": segment_index + 1, "packet_count": count,
                                "pts_offset_seconds": pts_median, "pts_offset_span_seconds": pts_span,
                                "dts_offset_span_seconds": dts_span,
                                "inferred_dts_prefix_packets": inferred_prefix, "passed": True})
        packet_checks.append({
            "source_stream_index": int(source_stream["index"]),
            "candidate_stream_index": int(candidate_stream["index"]),
            "role": _track_role(source_stream),
            "candidate_packet_count": len(actual_hashes), "expected_packet_count": len(expected_hashes),
            "payload_sequence_matches": True, "segments": offset_rows,
        })

    av_checks: list[dict[str, Any]] = []
    video_streams = [stream for stream in source_probe.get("streams") or []
                     if stream.get("codec_type") == "video" and not int((stream.get("disposition") or {}).get("attached_pic") or 0)]
    audio_streams = [stream for stream in source_probe.get("streams") or [] if stream.get("codec_type") == "audio"]
    if video_streams and audio_streams:
        for index, offsets in enumerate(segment_offsets):
            video_keys = [str(stream["index"]) for stream in video_streams if str(stream["index"]) in offsets]
            audio_keys = [str(stream["index"]) for stream in audio_streams if str(stream["index"]) in offsets]
            if not video_keys or not audio_keys:
                raise UnsupportedMediaError(f"第 {index + 1} 段缺少视频或音频时间戳，无法验证音画同步。")
            relevant = [offsets[key] for key in video_keys + audio_keys]
            spread = max(relevant) - min(relevant)
            passed = spread <= AV_SYNC_TOLERANCE_SECONDS
            av_checks.append({"segment": index + 1,
                              "video_audio_pts_offset_spread_seconds": spread,
                              "tolerance_seconds": AV_SYNC_TOLERANCE_SECONDS, "passed": passed})
            if not passed:
                raise UnsupportedMediaError(f"拼接后第 {index + 1} 段音画时间偏移不一致：{json.dumps(offsets, ensure_ascii=False)}，spread={spread:.6f}s。")

    decodes = []
    for stream in final_probe.get("streams") or []:
        if stream.get("codec_type") in ("video", "audio"):
            decodes.append(_ffmpeg_decode(candidate_path, stream, ffmpeg, job_dir, cancel_event, progress))
    if not all(item["passed"] for item in decodes):
        raise UnsupportedMediaError("最终候选未能完整解码所有视频和音频轨。")
    return {
        "passed": True,
        "stream_count_source": len(source_probe.get("streams") or []),
        "stream_count_candidate": len(final_probe.get("streams") or []),
        "metadata": metadata, "packet_checks": packet_checks, "av_sync_checks": av_checks,
        "full_decode": {"passed": True, "streams": decodes},
        "packet_hash_algorithm": "SHA-256",
        "packet_timestamp_tolerance_seconds": PACKET_TOLERANCE_SECONDS,
        "av_sync_tolerance_seconds": AV_SYNC_TOLERANCE_SECONDS,
    }


def _sha256(path: Path, cancel_event: threading.Event | None = None,
            progress: Progress | None = None, label: str = "计算 SHA-256") -> str:
    digest = hashlib.sha256()
    size = path.stat().st_size
    consumed = 0
    with path.open("rb") as stream:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TrimCancelled("用户已取消准备；原素材未修改。")
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            consumed += len(block)
            if progress and size:
                progress(f"{label} · {min(100, consumed * 100 // size)}%")
    return digest.hexdigest().upper()


def _job_directory(source_path: Path, work_dir: Path) -> Path:
    digest = hashlib.sha256(str(source_path.resolve()).casefold().encode("utf-8", "surrogatepass")).hexdigest()[:10]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source_path.stem).strip("-.") or "video"
    parent = work_dir.expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    job_dir = parent / f"{stem}-{digest}" / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    return job_dir


def _merge_keyframe_overlaps(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid duplicate media when backward keyframe alignment overlaps a range."""
    merged: list[dict[str, Any]] = []
    for row in ranges:
        current = row.copy()
        if merged and current["actual_start_s"] <= merged[-1]["requested_end_s"] + 0.001:
            previous = merged[-1]
            previous["requested_end_ms"] = max(previous["requested_end_ms"], current["requested_end_ms"])
            previous["requested_end_s"] = max(previous["requested_end_s"], current["requested_end_s"])
            previous["merged_requested_ranges"].extend(current["merged_requested_ranges"])
        else:
            merged.append(current)
    return merged


def _source_media_type(path: Path) -> str:
    extension = path.suffix.casefold()
    if extension in GPAC_EXTENSIONS:
        return "gpac"
    if extension in FFMPEG_COPY_EXTENSIONS:
        return "ffmpeg"
    raise UnsupportedMediaError(f"暂不支持 {extension or '无扩展名'} 容器的安全无损裁剪。原片未修改。")


def _minimal_probe(probe: dict[str, Any]) -> dict[str, Any]:
    streams = []
    for stream in probe.get("streams") or []:
        streams.append({"index": stream.get("index"), "id": stream.get("id"),
                        "codec_type": stream.get("codec_type"), "codec_name": stream.get("codec_name"),
                        "codec_tag_string": stream.get("codec_tag_string"), "width": stream.get("width"),
                        "height": stream.get("height"), "sample_rate": stream.get("sample_rate"),
                        "channels": stream.get("channels"), "disposition": stream.get("disposition"),
                        "rotation": _rotation(stream)})
    return {"format_name": (probe.get("format") or {}).get("format_name"),
            "duration": (probe.get("format") or {}).get("duration"),
            "format_tags": (probe.get("format") or {}).get("tags") or {}, "streams": streams}


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _cleanup_owned_paths(paths: list[str | Path]) -> list[str]:
    failures = []
    for raw in paths:
        path = Path(raw)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            failures.append(f"{path}: {error}")
    return failures


def prepare_segments(source: dict[str, Any], ranges: list[dict[str, Any]], work_dir: Path,
                     tools: dict, progress: Progress | None = None,
                     cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Prepare a verified candidate from one or more user-marked keep ranges.

    Ranges are ordered and overlapping ranges merged. Each start is moved
    backward to the preceding source keyframe. MP4/MOV/M4V use GPAC for each
    split and lossless concatenation; supported other containers use FFmpeg
    stream-copy. All output and scratch files live under a unique work_dir
    subdirectory. The input source is never opened for writing.
    """
    if not isinstance(source, dict) or not source.get("path"):
        raise ValueError("source 必须是 inspect_video 返回的快照。")
    source_path = Path(source["path"]).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到原始素材: {source_path}")
    _check_cancel(cancel_event)
    before_ok, source_stat, snapshot_errors = _snapshot_matches(source, source_path)
    if not before_ok:
        raise RuntimeError(f"原片自列表读取后发生变化: {', '.join(snapshot_errors)}")
    source_sha256 = _sha256(source_path, cancel_event, progress, label="计算原片 SHA-256")
    saved_content_sha = source.get("content_sha256") or source.get("source_sha256")
    if saved_content_sha and str(saved_content_sha).upper() != source_sha256:
        raise RuntimeError("原片内容指纹与扫描时的素材快照不一致，拒绝裁剪。")
    hash_snapshot_ok, source_stat, snapshot_errors = _snapshot_matches(source, source_path)
    if not hash_snapshot_ok:
        raise RuntimeError(f"计算原片指纹期间原片发生变化: {', '.join(snapshot_errors)}")

    ffmpeg = _tool_path(tools, "ffmpeg")
    ffprobe = _tool_path(tools, "ffprobe")
    media_type = _source_media_type(source_path)
    mp4box = _tool_path(tools, "mp4box", required=(media_type == "gpac"))
    assert ffmpeg is not None and ffprobe is not None
    source_probe = _probe(source_path, ffprobe, cancel_event)
    duration_s = _duration_seconds(source_probe)
    duration_ms = int(round(duration_s * 1000))
    if duration_ms <= 0:
        raise ValueError("无法取得原片时长。")
    normalized = _normalise_ranges(ranges, duration_ms)
    for interval in normalized:
        if interval["end_ms"] / 1000.0 > duration_s + 0.001:
            raise ValueError("保留区间结束位置超出视频时长。")

    job_dir = _job_directory(source_path, Path(work_dir))
    report_path = job_dir / "prepare-report.json"
    owned_media: list[Path] = []
    candidate_path = job_dir / f"candidate{source_path.suffix}"
    owned_media.append(candidate_path)
    report: dict[str, Any] = {
        "schema_version": 1, "status": "preparing", "source_path": str(source_path),
        "source_snapshot": source_stat, "source_sha256": source_sha256,
        "source_probe": _minimal_probe(source_probe),
        "requested_ranges": normalized, "media_engine": media_type,
        "created_at": datetime.now().astimezone().isoformat(), "segments": [], "verified": False,
    }
    try:
        _write_report(report_path, report)
        keyframe_ranges: list[dict[str, Any]] = []
        for index, interval in enumerate(normalized, start=1):
            _check_cancel(cancel_event)
            _notify(progress, f"定位第 {index}/{len(normalized)} 段关键帧")
            start_s, end_s = interval["start_ms"] / 1000.0, interval["end_ms"] / 1000.0
            actual_start_s = _source_keyframe(
                source_path, source_probe, start_s, ffprobe, job_dir,
                job_dir / f"keyframe-{index:02d}.log", cancel_event, progress,
            )
            keyframe_ranges.append({
                "requested_start_ms": interval["start_ms"], "requested_end_ms": interval["end_ms"],
                "requested_start_s": start_s, "requested_end_s": end_s,
                "actual_start_s": actual_start_s, "merged_requested_ranges": [interval.copy()],
            })
        keyframe_ranges = _merge_keyframe_overlaps(keyframe_ranges)
        for index, interval in enumerate(keyframe_ranges, start=1):
            interval["index"] = index

        split_paths: list[Path] = []
        segment_proofs: list[dict[str, Any]] = []
        for index, interval in enumerate(keyframe_ranges, start=1):
            _check_cancel(cancel_event)
            split_path = job_dir / f"segment-{index:02d}{source_path.suffix}"
            owned_media.append(split_path)
            if media_type == "gpac":
                assert mp4box is not None
                _gpac_split(source_path, split_path, interval["actual_start_s"], interval["requested_end_s"],
                            mp4box, job_dir, index, cancel_event, progress)
            else:
                _ffmpeg_segment(source_path, split_path, interval["actual_start_s"], interval["requested_end_s"],
                                ffmpeg, job_dir, index, cancel_event, progress)
            _notify(progress, f"核验第 {index}/{len(keyframe_ranges)} 段原片数据包")
            proof = _verify_split_segment(source_path, source_probe, split_path, index, interval,
                                          ffprobe, job_dir, cancel_event, progress)
            proof["split_path"] = str(split_path)
            proof["range"] = _actual_range_from_proof(proof, interval, index)
            actual = proof["range"]
            if (actual["actual_start_s"] > interval["requested_start_s"] + PACKET_TOLERANCE_SECONDS
                    or actual["actual_end_s"] < interval["requested_end_s"] - PACKET_TOLERANCE_SECONDS):
                raise UnsupportedMediaError(f"第 {index} 段未能完整覆盖标记范围，已拒绝裁剪。原片未修改。")
            if segment_proofs and segment_proofs[-1]["range"]["actual_end_s"] > actual["actual_start_s"] + 0.001:
                raise UnsupportedMediaError(
                    "容器实际对齐后，两个保留区间出现重叠。请将这两段改为一个保留区间后重试，原片未修改。"
                )
            segment_proofs.append(proof)
            split_paths.append(split_path)
            report["segments"].append({
                **proof["range"], "stream_count": len(proof["probe"].get("streams") or []),
                "stream_payload_checks_passed": True,
                "segment_pts_offset_seconds": proof["primary_offset_seconds"],
            })
            _write_report(report_path, report)

        _check_cancel(cancel_event)
        _notify(progress, "无损拼接全部保留区间")
        if media_type == "gpac":
            assert mp4box is not None
            concat_paths = split_paths.copy()
            for index in range(1, len(split_paths)):
                stripped = _gpac_strip_attached_tracks(
                    split_paths[index], source_probe, mp4box, job_dir, index + 1, cancel_event, progress,
                )
                if stripped is not None:
                    owned_media.append(stripped)
                    concat_paths[index] = stripped
            joined_raw = job_dir / f"joined-remux{source_path.suffix}"
            owned_media.append(joined_raw)
            _gpac_concat(concat_paths, joined_raw, mp4box, job_dir, cancel_event, progress)
            _check_cancel(cancel_event)
            report["metadata_restore"] = restore_gpac_metadata(source_path, joined_raw, candidate_path)
            report["header_restore"] = restore_movie_header_fields(source_path, candidate_path)
        else:
            _ffmpeg_concat(split_paths, candidate_path, ffmpeg, job_dir, cancel_event, progress,
                           source_path, source_probe)

        if not candidate_path.is_file() or candidate_path.stat().st_size <= 0:
            raise RuntimeError("没有生成最终裁剪候选。")
        _check_cancel(cancel_event)
        _notify(progress, "验证拼接后的轨道、时间轴和完整解码")
        final_probe = _probe(candidate_path, ffprobe, cancel_event)
        verification = _verify_final_candidate(
            source_path, source_probe, candidate_path, segment_proofs, final_probe,
            ffprobe, ffmpeg, job_dir, cancel_event, progress,
        )
        source_sha_after = _sha256(source_path, cancel_event, progress, label="复核原片 SHA-256")
        if source_sha_after != source_sha256:
            raise RuntimeError("准备期间原片内容发生变化；候选已清理，原片未修改。")
        after_ok, after_stat, after_errors = _snapshot_matches(source, source_path)
        if not after_ok:
            raise RuntimeError(f"准备期间原片发生变化: {', '.join(after_errors)}")
        sha256 = _sha256(candidate_path, cancel_event, progress, label="计算候选 SHA-256")
        candidate_size = candidate_path.stat().st_size
        actual_ranges = [segment["range"] for segment in segment_proofs]
        actual_start_ms = min(item["actual_start_ms"] for item in actual_ranges)
        actual_end_ms = max(item["actual_end_ms"] for item in actual_ranges)
        intermediate_paths = [path for path in owned_media if path != candidate_path]
        prepare_cleanup_errors = _cleanup_owned_paths(intermediate_paths)
        owned_media = [candidate_path] + [path for path in intermediate_paths if path.exists()]
        report["prepare_cleanup_errors"] = prepare_cleanup_errors
        result: dict[str, Any] = {
            "state": "prepared", "candidate_path": str(candidate_path), "source_path": str(source_path),
            "source_size_bytes": int(source_stat["size_bytes"]), "source_mtime_ns": int(source_stat["mtime_ns"]),
            "source_atime_ns": int(source_stat["atime_ns"]), "source_ctime_ns": int(source_stat["ctime_ns"]),
            "source_birthtime_ns": source_stat["birthtime_ns"], "source_file_id": source_stat["file_id"],
            "source_device_id": source_stat["device_id"], "source_sha256": source_sha256,
            "requested_start_ms": normalized[0]["start_ms"], "requested_end_ms": normalized[-1]["end_ms"],
            "requested_ranges": normalized, "actual_ranges": actual_ranges,
            "actual_start_ms": actual_start_ms, "actual_end_ms": actual_end_ms,
            "duration_ms": int(round(_duration_seconds(final_probe) * 1000)),
            "candidate_size_bytes": candidate_size, "saved_bytes": int(source_stat["size_bytes"] - candidate_size),
            "sha256": sha256, "verified": True, "verification": verification,
            "report_path": str(report_path), "work_directory": str(job_dir),
            "temporary_media_paths": [str(path) for path in owned_media],
            "source_after_prepare": after_stat,
            "prepare_cleanup_errors": prepare_cleanup_errors,
            "tool_paths": {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "mp4box": mp4box},
        }
        report.update({"status": "verified", "verified": True, "candidate_path": str(candidate_path),
                       "temporary_media_paths": [str(path) for path in owned_media],
                       "candidate_size_bytes": candidate_size, "candidate_sha256": sha256,
                       "actual_ranges": actual_ranges, "verification": verification,
                       "source_after_prepare": after_stat})
        _write_report(report_path, report)
        return result
    except TrimCancelled as error:
        pending_paths = [str(path) for path in owned_media + list(job_dir.glob("*.tmp"))]
        cleanup_errors = _cleanup_owned_paths(owned_media + list(job_dir.glob("*.tmp")))
        report.update({"status": "cancelled", "verified": False, "error": str(error),
                       "temporary_media_paths": [path for path in pending_paths if Path(path).exists()],
                       "cleanup_errors": cleanup_errors})
        _write_report(report_path, report)
        raise
    except Exception as error:
        pending_paths = [str(path) for path in owned_media + list(job_dir.glob("*.tmp"))]
        cleanup_errors = _cleanup_owned_paths(owned_media + list(job_dir.glob("*.tmp")))
        report.update({"status": "failed", "verified": False,
                       "error": f"{type(error).__name__}: {error}",
                       "temporary_media_paths": [path for path in pending_paths if Path(path).exists()],
                       "cleanup_errors": cleanup_errors})
        _write_report(report_path, report)
        raise


def prepare_trim(source: dict[str, Any], start_ms: int, end_ms: int, work_dir: Path,
                 tools: dict, progress: Progress | None = None,
                 cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Single-range compatibility wrapper over prepare_segments."""
    return prepare_segments(source, [{"start_ms": int(start_ms), "end_ms": int(end_ms)}],
                            work_dir, tools, progress=progress, cancel_event=cancel_event)


def _set_windows_birthtime(path: Path, birthtime_ns: int) -> None:
    if os.name != "nt":
        raise OSError("当前平台不能通过标准接口设置文件创建时间。")
    import ctypes
    from ctypes import wintypes

    FILE_WRITE_ATTRIBUTES = 0x0100
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 1, 2, 4
    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 3, 0x80
    ticks = int(birthtime_ns // 100 + 116444736000000000)
    file_time = wintypes.FILETIME(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), FILE_WRITE_ATTRIBUTES,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                         None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        set_file_time = kernel32.SetFileTime
        set_file_time.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        set_file_time.restype = wintypes.BOOL
        if not set_file_time(handle, ctypes.byref(file_time), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def _verify_candidate_for_commit(result: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
    if result.get("verified") is not True or result.get("state") != "prepared":
        raise ValueError("只允许提交已验证且处于 prepared 状态的候选。")
    source = Path(result["source_path"]).expanduser().resolve()
    candidate = Path(result["candidate_path"]).expanduser().resolve()
    if source == candidate:
        raise ValueError("候选路径不能与原片相同。")
    if not source.is_file() or not candidate.is_file():
        raise FileNotFoundError("原片或已验证候选不存在。")
    stat = _stat_info(source)
    for key, field in (("source_size_bytes", "size_bytes"), ("source_mtime_ns", "mtime_ns")):
        if int(result.get(key, -1)) != int(stat[field]):
            raise RuntimeError(f"原片 {field} 在准备后发生变化，拒绝提交。")
    expected_birthtime = result.get("source_birthtime_ns")
    if expected_birthtime is not None and stat["birthtime_ns"] is not None:
        if int(expected_birthtime) != int(stat["birthtime_ns"]):
            raise RuntimeError("原片创建时间在准备后发生变化，拒绝提交。")
    expected_file_id = result.get("source_file_id")
    if expected_file_id not in (None, "") and str(expected_file_id) != stat["file_id"]:
        raise RuntimeError("原片文件身份在准备后发生变化，拒绝提交。")
    expected_source_sha = str(result.get("source_sha256") or "").upper()
    if len(expected_source_sha) != 64 or _sha256(source) != expected_source_sha:
        raise RuntimeError("原片内容指纹在准备后发生变化，拒绝提交。")
    if int(result.get("candidate_size_bytes", -1)) != candidate.stat().st_size:
        raise RuntimeError("候选文件大小在验证后发生变化，拒绝提交。")
    expected_sha = str(result.get("sha256") or "").upper()
    if len(expected_sha) != 64 or _sha256(candidate) != expected_sha:
        raise RuntimeError("候选 SHA-256 与验证记录不符，拒绝提交。")
    return source, candidate, stat


def _unique_export_target(source: Path, export_dir: Path) -> Path:
    first = export_dir / source.name
    if not first.exists():
        return first
    index = 2
    while True:
        candidate = export_dir / f"{source.stem}_{index}{source.suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _copy_export(candidate: Path, source: Path, export_dir: Path,
                 source_stat: dict[str, Any], expected_sha: str) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    temporary = export_dir / f".{source.stem}.{uuid.uuid4().hex}.partial{source.suffix}"
    target: Path | None = None
    try:
        with candidate.open("rb") as inp, temporary.open("xb") as out:
            shutil.copyfileobj(inp, out, length=8 * 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.utime(temporary, ns=(int(source_stat["atime_ns"]), int(source_stat["mtime_ns"])))
        if os.name == "nt" and source_stat.get("birthtime_ns") is not None:
            _set_windows_birthtime(temporary, int(source_stat["birthtime_ns"]))
        while True:
            target = _unique_export_target(source, export_dir)
            try:
                os.link(temporary, target)
                break
            except FileExistsError:
                continue
            except OSError:
                if os.name == "nt":
                    try:
                        os.rename(temporary, target)
                        temporary = target
                        break
                    except FileExistsError:
                        continue
                descriptor = None
                try:
                    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                    with os.fdopen(descriptor, "wb") as out, candidate.open("rb") as inp:
                        descriptor = None
                        shutil.copyfileobj(inp, out, length=8 * 1024 * 1024)
                        out.flush()
                        os.fsync(out.fileno())
                    break
                except FileExistsError:
                    continue
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        if _sha256(target) != expected_sha:
            raise RuntimeError("导出文件 SHA-256 校验失败。")
        return target
    except Exception:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        if temporary.exists() and temporary != target:
            temporary.unlink(missing_ok=True)


def _compact_metadata(path: Path, tools: dict) -> dict[str, Any]:
    metadata = inspect_video(path, tools)
    return {key: metadata.get(key) for key in (
        "path", "name", "size_bytes", "mtime_ns", "birthtime_ns", "duration_ms",
        "width", "height", "frame_rate", "codec", "rotation", "streams",
    )}


def _cleanup_result_media(result: dict[str, Any], candidate: Path) -> list[str]:
    paths = [Path(path) for path in result.get("temporary_media_paths") or []]
    if candidate not in paths:
        paths.append(candidate)
    return _cleanup_owned_paths(paths)


def _probe_tool_paths(result: dict[str, Any]) -> dict[str, str]:
    paths = result.get("tool_paths") or {}
    ffprobe = paths.get("ffprobe") or _tool_path(None, "ffprobe")
    if not ffprobe:
        raise FileNotFoundError("提交后核对元数据需要 ffprobe。")
    return {"ffprobe": str(ffprobe)}


def commit_trim(result: dict[str, Any], mode: str, confirmed: bool = False,
                progress: Progress | None = None) -> dict[str, Any]:
    """Export a verified candidate or atomically replace its unchanged source.

    Replace requires explicit confirmation and a same-volume candidate. It
    keeps hard-link recovery points until hash, timestamps, and media metadata
    have been checked after replacement.
    """
    if mode not in {"replace", "export"}:
        raise ValueError("mode 必须为 replace 或 export。")
    if mode == "replace" and confirmed is not True:
        raise PermissionError("替换原片必须先由用户明确确认。")
    source, candidate, source_stat = _verify_candidate_for_commit(result)
    tools = _probe_tool_paths(result)
    _notify(progress, "再次检查原片与候选文件")
    expected_sha = str(result["sha256"]).upper()

    if mode == "export":
        export_dir = source.parent / "已裁剪"
        _notify(progress, "导出已验证的裁剪文件")
        final_path = _copy_export(candidate, source, export_dir, source_stat, expected_sha)
        try:
            source_snapshot = {
                "size_bytes": result["source_size_bytes"], "mtime_ns": result["source_mtime_ns"],
                "birthtime_ns": result.get("source_birthtime_ns"), "file_id": result.get("source_file_id"),
            }
            source_unchanged, source_after, source_errors = _snapshot_matches(source_snapshot, source)
            if not source_unchanged:
                raise RuntimeError(f"导出期间原片发生变化: {', '.join(source_errors)}")
            if _sha256(source) != str(result["source_sha256"]).upper():
                raise RuntimeError("导出期间原片内容发生变化。")
            final_hash = _sha256(final_path)
            if final_hash != expected_sha:
                raise RuntimeError("导出文件 SHA-256 校验失败。")
            final_metadata = _compact_metadata(final_path, tools)
            source_metadata_after = _compact_metadata(source, tools)
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        cleanup_errors = _cleanup_result_media(result, candidate)
        output = dict(result)
        output.update({
            "mode": "export", "final_path": str(final_path), "final_sha256": final_hash,
            "final_metadata": final_metadata, "source_metadata_after": source_metadata_after,
            "source_after_commit": source_after, "committed": True, "state": "committed",
            "temporary_media_paths": [path for path in result.get("temporary_media_paths", [])
                                       if Path(path).exists()],
            "cleanup_errors": cleanup_errors,
        })
        _update_report(output)
        return output

    _notify(progress, "原位替换已确认的原片")
    if int(source_stat["device_id"]) != int(candidate.stat().st_dev):
        raise OSError("原片和候选不在同一卷，无法原子替换。")
    if os.name == "nt" and source_stat.get("birthtime_ns") is None:
        raise OSError("无法读取原片创建时间，拒绝替换以免无法保留文件时间。")
    job_dir = Path(result.get("work_directory") or candidate.parent)
    rollback_path = job_dir / f"rollback-original-{uuid.uuid4().hex}.link"
    candidate_hold_path = job_dir / f"rollback-candidate-{uuid.uuid4().hex}.link"
    backup_created = False
    candidate_hold_created = False
    moved = False
    recovery_safe = True
    try:
        # Hard links add no media copy. They let us restore either side if the
        # post-rename verification fails unexpectedly.
        os.link(source, rollback_path)
        backup_created = True
        os.link(candidate, candidate_hold_path)
        candidate_hold_created = True
        os.utime(candidate, ns=(int(source_stat["atime_ns"]), int(source_stat["mtime_ns"])))
        if os.name == "nt" and source_stat.get("birthtime_ns") is not None:
            _set_windows_birthtime(candidate, int(source_stat["birthtime_ns"]))
        if _sha256(rollback_path) != str(result["source_sha256"]).upper():
            raise RuntimeError("原片在替换窗口内发生变化，拒绝原位替换。")
        if _sha256(candidate) != expected_sha:
            raise RuntimeError("候选在替换前的时间戳设置后发生变化。")
        os.replace(candidate, source)
        moved = True
        if _sha256(source) != expected_sha:
            raise RuntimeError("原位替换后的文件 SHA-256 不符。")
        final_metadata = _compact_metadata(source, tools)
        final_stat = _stat_info(source)
        if final_stat["mtime_ns"] != int(source_stat["mtime_ns"]):
            raise RuntimeError("替换后的文件修改时间未能恢复。")
        if source_stat.get("birthtime_ns") is not None and final_stat.get("birthtime_ns") != source_stat["birthtime_ns"]:
            raise RuntimeError("替换后的文件创建时间未能恢复。")
    except Exception as error:
        if moved:
            try:
                if backup_created and rollback_path.exists():
                    os.replace(rollback_path, source)
                    backup_created = False
                else:
                    raise RuntimeError("找不到原片回滚硬链接。")
            except Exception:
                recovery_safe = False
            try:
                if candidate_hold_created and candidate_hold_path.exists():
                    os.replace(candidate_hold_path, candidate)
                    candidate_hold_created = False
            except Exception:
                recovery_safe = False
        if not recovery_safe:
            raise RuntimeError(
                f"提交后核验失败且自动回滚不完整；请保留恢复链接 {rollback_path} 和 {candidate_hold_path}: {error}"
            ) from error
        raise
    finally:
        if recovery_safe:
            for path, active in ((rollback_path, backup_created), (candidate_hold_path, candidate_hold_created)):
                if active:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

    cleanup_errors = _cleanup_result_media(result, candidate)
    for path in (rollback_path, candidate_hold_path):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            cleanup_errors.append(f"{path}: {error}")
    output = dict(result)
    output.update({
        "mode": "replace", "final_path": str(source), "final_sha256": expected_sha,
        "final_metadata": final_metadata, "source_metadata_after": final_metadata,
        "source_after_commit": final_stat, "committed": True, "state": "committed",
        "temporary_media_paths": [path for path in result.get("temporary_media_paths", [])
                                   if Path(path).exists()],
        "cleanup_errors": cleanup_errors,
        "creation_time_preserved": source_stat.get("birthtime_ns") is None
                                    or final_stat.get("birthtime_ns") == source_stat.get("birthtime_ns"),
        "modification_time_preserved": final_stat["mtime_ns"] == int(source_stat["mtime_ns"]),
    })
    _update_report(output)
    return output


def discard_trim(result: dict[str, Any]) -> dict[str, Any]:
    """Discard only this engine invocation's temporary candidate media."""
    if result.get("state") != "prepared" or result.get("verified") is not True:
        raise ValueError("只允许放弃处于 prepared 状态的已验证候选。")
    source = Path(result["source_path"]).expanduser().resolve()
    candidate = Path(result["candidate_path"]).expanduser().resolve()
    work_dir = Path(result.get("work_directory") or "").expanduser().resolve()
    if source == candidate or not work_dir.is_dir():
        raise ValueError("候选路径或任务目录无效，拒绝清理。")
    if not candidate.is_relative_to(work_dir):
        raise ValueError("候选路径不在本次引擎任务目录内，拒绝清理。")
    paths = [Path(raw).expanduser().resolve() for raw in result.get("temporary_media_paths") or []]
    if candidate not in paths:
        paths.append(candidate)
    if any(path == source or not path.is_relative_to(work_dir) for path in paths):
        raise ValueError("待清理路径越过本次引擎任务目录，拒绝清理。")
    cleanup_errors = _cleanup_owned_paths(paths)
    output = dict(result)
    output.update({
        "state": "discarded", "verified": False, "discarded": True, "committed": False,
        "temporary_media_paths": [str(path) for path in paths if path.exists()],
        "cleanup_errors": cleanup_errors,
    })
    _update_report(output)
    return output


def _update_report(result: dict[str, Any]) -> None:
    report_path = result.get("report_path")
    if not report_path:
        return
    path = Path(report_path)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        current.update({
            "status": result.get("state", "committed"), "committed": bool(result.get("committed")),
            "mode": result.get("mode"), "final_path": result.get("final_path"),
            "final_sha256": result.get("final_sha256"), "cleanup_errors": result.get("cleanup_errors", []),
        })
        _write_report(path, current)
    except OSError:
        # Do not undo a verified commit just because an advisory report cannot
        # be refreshed; the in-memory result still records the final state.
        pass


__all__ = [
    "TrimCancelled", "UnsupportedMediaError", "inspect_video", "prepare_trim",
    "prepare_segments", "commit_trim", "discard_trim",
]
