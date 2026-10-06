"""Exercise the actual desktop controls and persisted multi-range editing."""
import sys
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from library import StateStore
from library import content_hash
from main import Controller
from window import TrimWindow
from tool_paths import resolve_tools


class DesktopEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)
        cls.fixture_root = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_root.name) / "fixture.mp4"
        tools = resolve_tools()
        subprocess.run([tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=128x72:rate=24:duration=60", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=60", "-c:v", "libx264", "-preset",
                        "ultrafast", "-g", "24", "-c:a", "aac", "-shortest", str(cls.fixture)],
                       check=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "测试视频.mp4"
        shutil.copy2(self.fixture, self.source)
        self.store = StateStore(self.root / "state")
        self.metadata = dict(path=str(self.source), name=self.source.name,
                             size_bytes=self.source.stat().st_size, mtime_ns=self.source.stat().st_mtime_ns,
                             duration_ms=60000, width=128, height=72, codec="h264", frame_rate=24, rotation=0)
        self.window = TrimWindow()
        self.controller = Controller(self.window, self.store, {})
        item = self.store.attach_saved(self.metadata)
        self.controller.items = {item["path"]: item}
        self.window.show()
        self.window.set_library(str(self.root), [item])
        QTest.qWait(60)
        self.controller.fingerprint_pool.waitForDone()
        self.application.processEvents()

    def tearDown(self):
        self.controller.fingerprint_pool.waitForDone()
        self.window.release_media()
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.temp.cleanup()

    def edit(self, start, end):
        for field, text in ((self.window.start_input, start), (self.window.end_input, end)):
            field.setFocus()
            field.selectAll()
            QTest.keyClicks(field, text)
            QTest.keyClick(field, Qt.Key.Key_Tab)
        QTest.qWait(20)

    def click(self, widget):
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        QTest.qWait(20)

    def test_multiple_ranges_update_delete_and_resume(self):
        self.edit("00:01.000", "00:06.000")
        self.click(self.window.add_range_button)
        self.click(self.window.new_range_button)
        self.edit("00:20.000", "00:30.000")
        self.click(self.window.add_range_button)
        self.assertEqual(self.window.range_list.count(), 2)
        self.controller.fingerprint_pool.waitForDone()
        restored = StateStore(self.root / "state").attach_saved(self.metadata)
        self.assertEqual(restored["ranges"], [{"start_ms": 1000, "end_ms": 6000},
                                             {"start_ms": 20000, "end_ms": 30000}])
        self.window.range_list.setCurrentRow(1)
        self.edit("00:21.000", "00:31.000")
        self.click(self.window.update_range_button)
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["ranges"][1], {"start_ms": 21000, "end_ms": 31000})
        self.click(self.window.delete_range_button)
        self.assertEqual(self.window.range_list.count(), 1)
        self.assertEqual(len(self.store.attach_saved(self.metadata)["ranges"]), 1)
        self.window.set_library(str(self.root), [self.store.attach_saved(self.metadata)])
        self.assertEqual(self.window.range_list.count(), 1)
        self.assertTrue(self.window.prepare_button.isEnabled())

    def test_source_hash_is_published_to_active_item_before_marking(self):
        item = self.controller.items[str(self.source)]
        self.assertEqual(item["content_sha256"], content_hash(self.source))
        self.assertFalse(item["fingerprint_pending"])
        self.assertTrue(self.window.mark_in_button.isEnabled())
        self.assertTrue(self.window.start_input.isEnabled())

    def test_empty_folder_and_single_partial_mark_do_not_enable_prepare(self):
        self.window.set_library(str(self.root), [])
        self.assertEqual(self.window.file_list.count(), 0)
        self.assertFalse(self.window.prepare_button.isEnabled())
        self.window.set_library(str(self.root), [self.store.attach_saved(self.metadata)])
        self.window.start_input.setText("00:04.000")
        self.window.start_input.editingFinished.emit()
        self.controller.fingerprint_pool.waitForDone()
        restored = self.store.attach_saved(self.metadata)
        self.assertEqual(restored["draft_start_ms"], 4000)
        self.assertIsNone(restored["draft_end_ms"])
        self.assertFalse(self.window.prepare_button.isEnabled())

    def test_adjacent_ranges_normalize_without_stale_rows(self):
        self.edit("00:01.000", "00:06.000")
        self.click(self.window.add_range_button)
        self.click(self.window.new_range_button)
        self.edit("00:06.000", "00:09.000")
        self.click(self.window.add_range_button)
        self.assertEqual(self.window.range_list.count(), 1)
        self.assertEqual(self.store.attach_saved(self.metadata)["ranges"],
                         [{"start_ms": 1000, "end_ms": 9000}])

    def test_overlapping_ranges_merge_in_actual_controls(self):
        self.edit("00:01.000", "00:06.000")
        self.click(self.window.add_range_button)
        self.click(self.window.new_range_button)
        self.edit("00:04.000", "00:09.000")
        self.click(self.window.add_range_button)
        self.controller.fingerprint_pool.waitForDone()
        self.assertEqual(self.window.range_list.count(), 1)
        self.assertEqual(self.store.attach_saved(self.metadata)["ranges"],
                         [{"start_ms": 1000, "end_ms": 9000}])

    def test_discard_removes_owned_media_and_preserves_source_and_external_files(self):
        owned = self.root / ".片段助手缓存" / "test-job"
        owned.mkdir(parents=True)
        candidate, intermediate = owned / "candidate.mp4", owned / "segment-01.mp4"
        outside = self.root / "unrelated.mp4"
        for path in (candidate, intermediate, outside):
            path.write_bytes(b"synthetic fixture")
        item = dict(self.metadata, result={"candidate_path": str(candidate),
                    "temporary_media_paths": [str(candidate), str(intermediate),
                                              str(self.source), str(outside)]})
        self.controller.discard_candidate(item)
        self.assertFalse(candidate.exists())
        self.assertFalse(intermediate.exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(outside.read_bytes(), b"synthetic fixture")


if __name__ == "__main__":
    unittest.main()
