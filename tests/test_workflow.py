"""Verify the desktop's prepare, preview, export and confirmation boundaries."""
import hashlib
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests import test_gui as gui
from tool_paths import resolve_tools


class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui.DesktopEditingTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        gui.DesktopEditingTests.tearDownClass.__func__(cls)

    setUp = gui.DesktopEditingTests.setUp
    tearDown = gui.DesktopEditingTests.tearDown
    edit = gui.DesktopEditingTests.edit
    click = gui.DesktopEditingTests.click

    def wait_job(self):
        deadline = time.monotonic() + 75
        while self.controller.job and time.monotonic() < deadline:
            QTest.qWait(40)
        self.assertIsNone(self.controller.job, "Background operation did not finish")

    def prepare_two_segments(self):
        self.controller.tools = resolve_tools()
        self.edit("00:01.000", "00:06.000")
        self.click(self.window.add_range_button)
        self.click(self.window.new_range_button)
        self.edit("00:20.000", "00:30.000")
        self.click(self.window.add_range_button)
        self.click(self.window.prepare_button)
        self.wait_job()
        item = self.controller.items[str(self.source)]
        self.assertEqual(item["state"], "prepared", item.get("error"))
        self.assertTrue(item["result"]["verified"])
        self.assertEqual(len(item["result"]["actual_ranges"]), 2)
        self.click(self.window.preview_result_button)
        self.assertTrue(self.window.preview_is_candidate)
        self.window.player.play()
        QTest.qWait(200)
        self.window.player.pause()
        self.assertTrue(self.window.player.hasVideo())
        self.assertTrue(self.window.player.hasAudio())
        return item["result"]

    def modal_answer(self, button):
        def answer():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                dialog.button(button).click()
            else:
                QTimer.singleShot(30, answer)
        QTimer.singleShot(40, answer)

    def test_cancelled_confirmation_then_replace_changes_only_synthetic_source(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest().upper()
        result = self.prepare_two_segments()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest().upper(), before)
        self.modal_answer(QMessageBox.StandardButton.Cancel)
        self.click(self.window.replace_button)
        self.assertIsNone(self.controller.job)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest().upper(), before)
        self.modal_answer(QMessageBox.StandardButton.Yes)
        self.click(self.window.replace_button)
        self.wait_job()
        item = self.controller.items[str(self.source)]
        self.assertEqual(item["state"], "completed", item.get("error"))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest().upper(), result["sha256"])
        self.assertFalse(Path(result["candidate_path"]).exists())
        self.assertFalse(list(Path(result["work_directory"]).glob("rollback*.link")))
        self.assertLess(item["duration_ms"], 17000)
        self.assertEqual(item["ranges"], [])

    def test_export_keeps_source_and_clears_consumed_candidate(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest().upper()
        result = self.prepare_two_segments()
        self.click(self.window.export_button)
        self.wait_job()
        item = self.controller.items[str(self.source)]
        self.assertEqual(item["state"], "marked", item.get("error"))
        self.assertIsNone(item["result"])
        exported = Path(item["last_receipt"]["final_path"])
        self.assertTrue(exported.is_file())
        self.assertEqual(hashlib.sha256(exported.read_bytes()).hexdigest().upper(), result["sha256"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest().upper(), before)
        self.assertFalse(Path(result["candidate_path"]).exists())
        self.assertFalse(self.window.replace_button.isVisible())


if __name__ == "__main__":
    unittest.main()
