"""Exercise directory refresh, row exclusion, and restoration through the Qt controller."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from library import StateStore, content_hash
from main import Controller
from tool_paths import resolve_tools
from window import TrimWindow


class DirectoryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)
        cls.tools = resolve_tools()
        cls.fixture_root = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_root.name) / "fixture.mp4"
        subprocess.run(
            [cls.tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=size=128x72:rate=12:duration=8", "-f", "lavfi", "-i",
             "sine=frequency=440:sample_rate=48000:duration=8", "-c:v", "libx264", "-preset",
             "ultrafast", "-g", "12", "-c:a", "aac", "-shortest", str(cls.fixture)],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture_root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "videos"
        self.folder.mkdir()
        self.first = self.folder / "01-first.mp4"
        self.second = self.folder / "02-second.mp4"
        shutil.copy2(self.fixture, self.first)
        shutil.copy2(self.fixture, self.second)
        self.state_directory = self.root / "state"
        self.store = StateStore(self.state_directory)
        self.windows = []
        self.controllers = []
        self.window, self.controller = self.make_controller(self.store)

    def tearDown(self):
        for controller in self.controllers:
            controller.fingerprint_pool.waitForDone()
            controller.pool.waitForDone()
        for window in self.windows:
            window.release_media()
            window.close()
            window.deleteLater()
        self.application.processEvents()
        self.temp.cleanup()

    def make_controller(self, store):
        window = TrimWindow()
        controller = Controller(window, store, self.tools)
        self.windows.append(window)
        self.controllers.append(controller)
        window.show()
        self.application.processEvents()
        return window, controller

    def wait_for_job(self, controller):
        deadline = time.monotonic() + 30
        while controller.job is not None and time.monotonic() < deadline:
            self.application.processEvents()
            QTest.qWait(20)
        self.application.processEvents()
        self.assertIsNone(controller.job, "目录扫描未能在 30 秒内完成")

    def wait_for_fingerprints(self, controller):
        deadline = time.monotonic() + 20
        while controller.fingerprinting and time.monotonic() < deadline:
            self.application.processEvents()
            QTest.qWait(20)
        self.application.processEvents()
        self.assertFalse(controller.fingerprinting, "后台指纹任务未能在 20 秒内完成")

    def scan(self, window, controller):
        window.open_folder_requested.emit(str(self.folder))
        self.wait_for_job(controller)
        self.wait_for_fingerprints(controller)

    @staticmethod
    def source_snapshot(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, content_hash(path)

    def test_refresh_exclusion_restart_and_restore_keep_saved_work_and_sources(self):
        snapshots = {path: self.source_snapshot(path) for path in (self.first, self.second)}

        self.scan(self.window, self.controller)
        first_key, second_key = str(self.first), str(self.second)
        self.assertEqual(set(self.controller.items), {first_key, second_key})
        self.assertEqual(self.window.current_path, first_key)
        self.assertTrue(self.controller.items[first_key].get("content_sha256"))

        ranges = [{"start_ms": 1000, "end_ms": 2500}, {"start_ms": 4000, "end_ms": 6000}]
        self.window.marks_changed.emit(first_key, {
            "ranges": ranges,
            "draft_start_ms": 6500,
            "draft_end_ms": None,
        })
        candidate = self.folder / ".片段助手缓存" / "synthetic-candidate.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"synthetic candidate only")
        marked_item = self.controller.items[first_key]
        marked_item.update(
            state="prepared",
            result={"candidate_path": str(candidate), "verified": True,
                    "actual_ranges": ranges, "duration_ms": 3500},
        )
        self.store.save_item(marked_item)
        self.controller.update_window_item(marked_item)

        self.window.refresh_requested.emit(str(self.folder))
        self.wait_for_job(self.controller)
        restored_in_memory = self.controller.items[first_key]
        self.assertEqual(self.window.current_path, first_key)
        self.assertEqual(restored_in_memory["ranges"], ranges)
        self.assertEqual((restored_in_memory["draft_start_ms"], restored_in_memory["draft_end_ms"]),
                         (6500, None))
        self.assertEqual(restored_in_memory["state"], "prepared")
        self.assertEqual(restored_in_memory["result"]["candidate_path"], str(candidate))
        self.assertTrue(candidate.is_file())

        self.window.remove_files_requested.emit([first_key])
        self.assertEqual(set(self.controller.items), {second_key})
        self.assertEqual(self.window.file_list.count(), 1)
        self.assertEqual(self.window.current_path, second_key)
        self.assertEqual(self.store.removed_count(self.folder), 1)
        self.assertTrue(self.first.is_file())
        self.assertTrue(candidate.is_file())

        # A fingerprint callback already in flight must not put an excluded row back.
        self.controller.receive_fingerprint({
            "snapshot": dict(marked_item),
            "digest": marked_item["content_sha256"],
        })
        self.assertEqual(set(self.controller.items), {second_key})
        self.wait_for_fingerprints(self.controller)

        self.window.refresh_requested.emit(str(self.folder))
        self.wait_for_job(self.controller)
        self.wait_for_fingerprints(self.controller)
        self.assertEqual(set(self.controller.items), {second_key})
        self.assertEqual(self.window.file_list.count(), 1)
        self.assertEqual(self.store.removed_count(self.folder), 1)
        self.assertTrue(candidate.is_file())

        reopened_store = StateStore(self.state_directory)
        reopened_window, reopened_controller = self.make_controller(reopened_store)
        self.scan(reopened_window, reopened_controller)
        self.assertEqual(set(reopened_controller.items), {second_key})
        self.assertEqual(reopened_store.removed_count(self.folder), 1)
        self.assertEqual(reopened_window.restore_files_button.text(), "恢复移除项（1）")
        saved_metadata = reopened_store.cached_metadata(self.first)
        self.assertIsNotNone(saved_metadata)
        hidden_saved_item = reopened_store.attach_saved(saved_metadata)
        self.assertEqual(hidden_saved_item["ranges"], ranges)
        self.assertEqual((hidden_saved_item["draft_start_ms"], hidden_saved_item["draft_end_ms"]),
                         (6500, None))
        self.assertEqual(hidden_saved_item["state"], "prepared")
        self.assertEqual(hidden_saved_item["result"]["candidate_path"], str(candidate))

        reopened_window.restore_files_requested.emit(str(self.folder))
        self.wait_for_job(reopened_controller)
        self.wait_for_fingerprints(reopened_controller)
        self.assertEqual(set(reopened_controller.items), {first_key, second_key})
        self.assertEqual(reopened_store.removed_count(self.folder), 0)
        restored_again = reopened_controller.items[first_key]
        self.assertEqual(restored_again["ranges"], ranges)
        self.assertEqual(restored_again["state"], "prepared")
        reopened_window.file_list.setCurrentRow(0)
        self.application.processEvents()
        self.assertEqual(reopened_window.current_path, first_key)
        self.assertEqual(reopened_window.range_list.count(), 2)
        self.assertTrue(candidate.is_file())

        self.assertEqual({path: self.source_snapshot(path) for path in (self.first, self.second)}, snapshots)


if __name__ == "__main__":
    unittest.main()
