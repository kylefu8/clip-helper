import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library import StateStore, content_hash, normalize_ranges


class SavedMarksTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "鱼获 0001.mp4"
        self.path.write_bytes(b"local test fixture")
        self.metadata = {"path": str(self.path), "name": self.path.name, "size_bytes": self.path.stat().st_size,
                         "mtime_ns": self.path.stat().st_mtime_ns, "duration_ms": 120000,
                         "width": 3840, "height": 2160, "frame_rate": 59.94, "codec": "hevc", "rotation": -90}
        self.metadata["content_sha256"] = content_hash(self.path)
        self.store = StateStore(self.root / "state")

    def tearDown(self):
        self.directory.cleanup()

    def test_marks_survive_relaunch_and_are_bound_to_source_snapshot(self):
        item = self.store.attach_saved(self.metadata)
        item.update(draft_start_ms=35500, draft_end_ms=95000,
                    ranges=[{"start_ms": 35500, "end_ms": 95000}], state="marked")
        self.store.save_item(item)
        reopened = StateStore(self.root / "state")
        restored = reopened.attach_saved(self.metadata)
        self.assertEqual((restored["start_ms"], restored["end_ms"]), (35500, 95000))
        changed = dict(self.metadata, size_bytes=self.metadata["size_bytes"] + 1)
        self.assertIsNone(reopened.attach_saved(changed)["start_ms"])
        self.assertEqual(reopened.attach_saved(changed)["state"], "unmarked")

    def test_lost_candidate_returns_to_marked_without_claiming_ready(self):
        item = self.store.attach_saved(self.metadata)
        item.update(draft_start_ms=1000, draft_end_ms=5000,
                    ranges=[{"start_ms": 1000, "end_ms": 5000}], state="prepared",
                    result={"candidate_path": str(self.root / "gone.mp4")})
        self.store.save_item(item)
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["state"], "marked")
        self.assertIsNone(restored["result"])

    def test_partial_marks_and_interrupted_job_are_restored(self):
        item = self.store.attach_saved(self.metadata)
        item.update(draft_start_ms=3000, state="preparing")
        self.store.save_item(item)
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["start_ms"], 3000)
        self.assertIsNone(restored["end_ms"])
        self.assertEqual(restored["state"], "unmarked")

    def test_invalid_saved_range_is_not_reused(self):
        item = self.store.attach_saved(self.metadata)
        item.update(draft_start_ms=10000, draft_end_ms=5000, state="marked")
        self.store.save_item(item)
        self.assertIsNone(self.store.attach_saved(self.metadata)["start_ms"])

    def test_multiple_ranges_and_draft_survive_restart(self):
        item = self.store.attach_saved(self.metadata)
        ranges = [{"start_ms": 1000, "end_ms": 3000}, {"start_ms": 9000, "end_ms": 15000}]
        item.update(ranges=ranges, draft_start_ms=18000, state="marked")
        self.store.save_item(item)
        restored = StateStore(self.root / "state").attach_saved(self.metadata)
        self.assertEqual(restored["ranges"], ranges)
        self.assertEqual(restored["draft_start_ms"], 18000)

    def test_overlapping_ranges_are_merged_in_source_order(self):
        ranges = [{"start_ms": 9000, "end_ms": 15000}, {"start_ms": 1000, "end_ms": 3000},
                  {"start_ms": 2000, "end_ms": 5000}]
        self.assertEqual(normalize_ranges(ranges, 20000),
                         [{"start_ms": 1000, "end_ms": 5000}, {"start_ms": 9000, "end_ms": 15000}])
        with self.assertRaises(ValueError):
            normalize_ranges([{"start_ms": 1, "end_ms": 20001}], 20000)

    def test_invalid_draft_does_not_discard_saved_segments(self):
        item = self.store.attach_saved(self.metadata)
        ranges = [{"start_ms": 1000, "end_ms": 3000}]
        item.update(ranges=ranges, draft_start_ms=9000, draft_end_ms=8000, state="marked")
        self.store.save_item(item)
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["ranges"], ranges)
        self.assertIsNone(restored["draft_start_ms"])

    def test_unbound_legacy_records_are_not_silently_reused(self):
        item = dict(self.metadata, start_ms=2000, end_ms=9000, state="marked")
        item.pop("content_sha256")
        self.store.save_item(item)
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["ranges"], [])

    def test_in_place_content_change_with_same_size_and_mtime_invalidates_marks(self):
        item = self.store.attach_saved(self.metadata)
        item.update(ranges=[{"start_ms": 1000, "end_ms": 4000}], state="marked",
                    content_sha256=content_hash(self.path))
        self.store.save_item(item)
        before = self.path.stat()
        self.path.write_bytes(b"x" * before.st_size)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIsNone(self.store.cached_metadata(self.path))
        self.assertEqual(self.store.attach_saved(self.metadata)["ranges"], [])

    def test_background_fingerprint_preserves_newer_range_edits(self):
        item = self.store.attach_saved(self.metadata)
        item.update(ranges=[{"start_ms": 1000, "end_ms": 4000}], state="marked", fingerprint_pending=True)
        self.store.save_item(item)
        snapshot = item.copy()
        item["ranges"] = [{"start_ms": 7000, "end_ms": 14000}]
        self.store.save_item(item)
        self.store.record_fingerprint(str(self.path), snapshot, content_hash(self.path))
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["ranges"], item["ranges"])
        self.assertEqual(restored["content_sha256"], content_hash(self.path))

    def test_corrupt_record_is_preserved_before_new_save(self):
        self.store.path.write_text("broken {", encoding="utf-8")
        reopened = StateStore(self.root / "state")
        self.assertTrue(reopened.warning)
        reopened.set_directory(self.root)
        backups = list((self.root / "state").glob("marks-unreadable-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "broken {")
        self.assertEqual(json.loads(reopened.path.read_text(encoding="utf-8"))["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
