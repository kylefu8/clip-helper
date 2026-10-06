from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from media_engine import TrimCancelled, UnsupportedMediaError, commit_trim, discard_trim, inspect_video, prepare_segments, prepare_trim

APP_ROOT = Path(__file__).resolve().parents[1]
ENGINE_TESTS = APP_ROOT / "work" / "engine-tests"
TOOLS = {
    "ffmpeg": str(APP_ROOT / "tools" / "ffmpeg.exe"),
    "ffprobe": str(APP_ROOT / "tools" / "ffprobe.exe"),
    "mp4box": str(APP_ROOT / "tools" / "gpac" / "mp4box.exe"),
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def run_fixture_encoder(command: list[str]) -> None:
    completed = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if completed.returncode:
        raise RuntimeError(f"fixture generation failed: {completed.stderr[-3000:]}")


@unittest.skipUnless(all(Path(value).is_file() for value in TOOLS.values()), "bundled media tools unavailable")
class MediaEngineTests(unittest.TestCase):
    def setUp(self):
        ENGINE_TESTS.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="fixture-", dir=ENGINE_TESTS)
        self.root = Path(self.temp.name)
        self.source_path = self.root / "sample.mp4"
        run_fixture_encoder([
            TOOLS["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=900:sample_rate=48000",
            "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-keyint_min", "30", "-sc_threshold", "0", "-bf", "2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(self.source_path),
        ])
        self.tools = TOOLS.copy()
        self.source = inspect_video(self.source_path, self.tools)
        self.work_dir = self.root / ".片段助手缓存"

    def tearDown(self):
        self.temp.cleanup()

    def prepare_two_segments(self):
        return prepare_segments(
            self.source,
            [{"start_ms": 6000, "end_ms": 8200}, {"start_ms": 1000, "end_ms": 3200}],
            self.work_dir, self.tools,
        )

    def test_inspect_and_prepare_multiple_ranges(self):
        self.assertEqual(self.source["name"], "sample.mp4")
        self.assertGreater(self.source["duration_ms"], 11000)
        self.assertEqual(self.source["codec"], "h264")
        result = self.prepare_two_segments()
        self.assertTrue(result["verified"])
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(len(result["actual_ranges"]), 2)
        self.assertLessEqual(result["actual_ranges"][0]["actual_start_ms"], 1000)
        self.assertLessEqual(result["actual_ranges"][1]["actual_start_ms"], 6000)
        self.assertTrue(Path(result["candidate_path"]).is_file())
        self.assertEqual(file_hash(Path(result["candidate_path"])), result["sha256"])
        self.assertTrue(all(item["payload_sequence_matches"] for item in result["verification"]["packet_checks"]))
        json.dumps(result, ensure_ascii=False)

    def test_single_range_compatibility_wrapper(self):
        result = prepare_trim(self.source, 2000, 5000, self.work_dir, self.tools)
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["actual_ranges"]), 1)
        discarded = discard_trim(result)
        self.assertTrue(discarded["discarded"])
        self.assertFalse(Path(result["candidate_path"]).exists())

    def test_export_leaves_source_and_avoids_existing_name(self):
        source_before = file_hash(self.source_path)
        source_stat = self.source_path.stat()
        result = prepare_trim(self.source, 2000, 5000, self.work_dir, self.tools)
        export_dir = self.source_path.parent / "已裁剪"
        export_dir.mkdir()
        collision = export_dir / self.source_path.name
        collision.write_bytes(b"leave this existing file alone")
        exported = commit_trim(result, "export")
        self.assertTrue(exported["committed"])
        self.assertEqual(Path(exported["final_path"]).name, "sample_2.mp4")
        self.assertEqual(file_hash(Path(exported["final_path"])), result["sha256"])
        self.assertEqual(collision.read_bytes(), b"leave this existing file alone")
        self.assertEqual(file_hash(self.source_path), source_before)
        self.assertEqual(self.source_path.stat().st_mtime_ns, source_stat.st_mtime_ns)
        self.assertFalse(Path(result["candidate_path"]).exists())

    def test_replace_requires_confirmation_then_replaces_fixture_only(self):
        source_mtime = self.source_path.stat().st_mtime_ns
        result = prepare_trim(self.source, 1000, 4000, self.work_dir, self.tools)
        before = file_hash(self.source_path)
        with self.assertRaises(PermissionError):
            commit_trim(result, "replace", confirmed=False)
        self.assertEqual(file_hash(self.source_path), before)
        committed = commit_trim(result, "replace", confirmed=True)
        self.assertTrue(committed["committed"])
        self.assertEqual(committed["final_sha256"], result["sha256"])
        self.assertEqual(file_hash(self.source_path), result["sha256"])
        self.assertEqual(self.source_path.stat().st_mtime_ns, source_mtime)
        self.assertFalse(Path(result["candidate_path"]).exists())
        self.assertEqual(committed["state"], "committed")

    def test_mkv_fallback_keeps_packet_sequences(self):
        source_path = self.root / "sample.mkv"
        run_fixture_encoder([
            TOOLS["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-i", str(self.source_path), "-map", "0", "-c", "copy", "-map_metadata", "0",
            "-metadata", "title=多段保留测试", "-metadata:s:a:0", "language=zho",
            "-metadata:s:v:0", "title=原视频轨", "-copy_unknown", str(source_path),
        ])
        metadata = inspect_video(source_path, self.tools)
        source_hash = file_hash(source_path)
        source_mtime = source_path.stat().st_mtime_ns
        result = prepare_segments(
            metadata,
            [{"start_ms": 6000, "end_ms": 8200}, {"start_ms": 1000, "end_ms": 3200}],
            self.root / ".片段助手缓存-mkv", self.tools,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["actual_ranges"]), 2)
        for row in result["actual_ranges"]:
            self.assertLessEqual(row["actual_start_ms"], row["requested_start_ms"])
            self.assertGreaterEqual(row["actual_end_ms"], row["requested_end_ms"])
        self.assertTrue(all(row["payload_sequence_matches"] for row in result["verification"]["packet_checks"]))
        self.assertTrue(result["verification"]["metadata"]["passed"])
        for check in result["verification"]["packet_checks"]:
            for segment in check["segments"]:
                self.assertLessEqual(segment["pts_offset_span_seconds"], 0.050)
                self.assertLessEqual(segment["dts_offset_span_seconds"], 0.050)
        self.assertTrue(all(check["passed"] for check in result["verification"]["av_sync_checks"]))
        self.assertEqual(file_hash(source_path), source_hash)
        self.assertEqual(source_path.stat().st_mtime_ns, source_mtime)
        exported = commit_trim(result, "export")
        self.assertTrue(exported["committed"])
        self.assertEqual(file_hash(Path(exported["final_path"])), result["sha256"])
        self.assertEqual(file_hash(source_path), source_hash)

    def test_same_size_source_change_with_restored_mtime_blocks_commit(self):
        result = prepare_trim(self.source, 1000, 4000, self.work_dir, self.tools)
        before = self.source_path.stat()
        payload = bytearray(self.source_path.read_bytes())
        payload[-1] ^= 0x01
        self.source_path.write_bytes(payload)
        os.utime(self.source_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.source_path.stat().st_size, before.st_size)
        self.assertEqual(self.source_path.stat().st_mtime_ns, before.st_mtime_ns)
        with self.assertRaises(RuntimeError):
            commit_trim(result, "export")
        self.assertTrue(Path(result["candidate_path"]).is_file())

    def test_container_seek_overlap_cannot_duplicate_source_frames(self):
        source_path = self.root / "sample.mkv"
        run_fixture_encoder([TOOLS["ffmpeg"], "-v", "error", "-y", "-i", str(self.source_path),
                             "-map", "0", "-c", "copy", str(source_path)])
        before_hash = file_hash(source_path)
        before_stat = source_path.stat()
        with self.assertRaisesRegex(UnsupportedMediaError, "重叠"):
            prepare_segments(inspect_video(source_path, self.tools),
                [{"start_ms": 1000, "end_ms": 4500}, {"start_ms": 6000, "end_ms": 8200}],
                self.root / ".片段助手缓存-overlap", self.tools)
        self.assertEqual(file_hash(source_path), before_hash)
        self.assertEqual(source_path.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_cached_content_hash_mismatch_blocks_prepare(self):
        source = dict(self.source)
        source["content_sha256"] = "0" * 64
        original_hash = file_hash(self.source_path)
        with self.assertRaises(RuntimeError):
            prepare_trim(source, 1000, 3000, self.work_dir, self.tools)
        self.assertEqual(file_hash(self.source_path), original_hash)

    def test_cancelled_prepare_keeps_source(self):
        original_hash = file_hash(self.source_path)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TrimCancelled):
            prepare_segments(self.source, [{"start_ms": 1000, "end_ms": 4000}],
                             self.work_dir, self.tools, cancel_event=cancel)
        self.assertEqual(file_hash(self.source_path), original_hash)


if __name__ == "__main__":
    unittest.main()
