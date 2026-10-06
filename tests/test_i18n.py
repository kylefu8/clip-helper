"""Verify bilingual controls, persisted preferences and unchanged editing state."""
import copy
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QLocale, QTimer, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import i18n
from translations import EN
from tests import test_gui as gui


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.old_language, self.old_settings = i18n._language, i18n._settings
        i18n._settings = None
        i18n.set_language("en")

    def tearDown(self):
        i18n._language, i18n._settings = self.old_language, self.old_settings

    def test_all_templates_preserve_fields_and_render(self):
        for source, target in EN.items():
            with self.subTest(source=source):
                self.assertEqual(sorted(re.findall(r"\{\d+\}", source)),
                                 sorted(re.findall(r"\{\d+\}", target)))
                fields = [int(n) for n in re.findall(r"\{(\d+)\}", source)]
                values = [f"value{n}" for n in range(max(fields, default=-1) + 1)]
                self.assertEqual(i18n.tr(source.format(*values)), target.format(*values))
                self.assertFalse(re.search(r"[\u4e00-\u9fff]", target))

    def test_nested_progress_errors_and_paths(self):
        self.assertEqual(i18n.tr("1/2 · 已标记.mp4 · 定位第 1/2 段关键帧"),
                         "1/2 · 已标记.mp4 · Locating keyframe for segment 1/2")
        self.assertEqual(i18n.tr("已导出 2 条视频。 保存位置：D:\\视频\\已完成"),
                         "Completed 2 videos · Action: Export. Saved to: D:\\视频\\已完成")
        self.assertEqual(i18n.tr("裁剪文件已保存，但部分临时文件未清除：\nfile locked"),
                         "The trimmed file was saved, but some temporary files could not be cleaned up:\nfile locked")
        self.assertEqual(i18n.tr("测试.mp4：结束必须晚于开始"),
                         "测试.mp4: End must be later than start")
        for path in ("D:\\视频\\未标记", "\\\\server\\共享\\视频", "/视频/已完成"):
            self.assertEqual(i18n.tr(path), path)

    def test_language_preference_system_default_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for language, expected in ((QLocale.Language.Chinese, "zh"),
                                       (QLocale.Language.English, "en")):
                with patch("i18n.QLocale.system", return_value=SimpleNamespace(language=lambda: language)):
                    i18n.configure(root)
                    self.assertEqual(i18n.get_language(), expected)
            i18n.set_language("zh")
            i18n.configure(root)
            self.assertEqual(i18n.get_language(), "zh")
            i18n.configure(root, "en")
            self.assertEqual(i18n.get_language(), "en")
            i18n.configure(root)
            self.assertEqual(i18n.get_language(), "zh")
        with self.assertRaises(ValueError):
            i18n.set_language("invalid")


class BilingualDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui.DesktopEditingTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        gui.DesktopEditingTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.old_language, self.old_settings = i18n._language, i18n._settings
        i18n._settings = None
        i18n.set_language("zh")
        gui.DesktopEditingTests.setUp(self)
        i18n.configure(self.root / "state", "zh")

    def tearDown(self):
        gui.DesktopEditingTests.tearDown(self)
        i18n._language, i18n._settings = self.old_language, self.old_settings

    edit = gui.DesktopEditingTests.edit
    click = gui.DesktopEditingTests.click

    def switch(self, language):
        self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
        self.application.processEvents()

    def test_switch_keeps_selection_ranges_draft_media_and_position(self):
        self.edit("00:01.000", "00:06.000")
        self.click(self.window.add_range_button)
        self.click(self.window.new_range_button)
        self.edit("00:20.000", "00:30.000")
        self.window.player.pause()
        self.window.player.setPosition(24000)
        QTest.qWait(120)
        before = copy.deepcopy(self.controller.items)
        saved = copy.deepcopy(self.store.data)
        media = self.window.player.source()
        position = self.window.player.position()
        selected = self.window.file_list.currentItem()
        for language, prepare in (("en", "Prepare marked videos (1)"),
                                  ("zh", "准备已标记的视频（1）")):
            self.switch(language)
            self.assertEqual(self.window.prepare_button.text(), prepare)
            self.assertEqual(self.window.start_input.text(), "00:20.000")
            self.assertEqual(self.window.end_input.text(), "00:30.000")
            self.assertEqual(self.window.player.source(), media)
            self.assertEqual(self.window.player.position(), position)
            self.assertEqual(self.window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
            self.assertIs(self.window.file_list.currentItem(), selected)
            self.assertEqual(self.controller.items, before)
            self.assertEqual(self.store.data, saved)
        i18n.configure(self.root / "state")
        self.assertEqual(i18n.get_language(), "zh")

    def test_switch_keeps_unsubmitted_text_and_busy_blocks_switch(self):
        self.window.start_input.setText("00:03.123")
        self.window.folder_input.setText(str(self.root / "中文视频目录"))
        self.switch("en")
        self.assertEqual(self.window.start_input.text(), "00:03.123")
        self.assertEqual(self.window.folder_input.text(), str(self.root / "中文视频目录"))
        self.window.set_busy(True)
        self.assertFalse(self.window.language_combo.isEnabled())
        self.switch("zh")
        self.assertEqual(i18n.get_language(), "en")
        self.assertEqual(self.window.language_combo.currentData(), "en")
        self.window.set_busy(False)
        self.switch("zh")
        self.assertTrue(self.window.language_combo.isEnabled())

    def test_dynamic_details_error_and_confirm_dialog_are_translated(self):
        self.edit("00:01.000", "00:06.000")
        self.click(self.window.add_range_button)
        self.switch("en")
        self.assertTrue(self.window.media_details_label.text().startswith("Landscape  128×72"))
        self.assertEqual(self.window.media_title_label.text(), "Original · 测试视频.mp4")
        self.window.show_error("结束必须晚于开始")
        self.assertEqual(self.window.feedback_label.text(), "End must be later than start")
        item = self.controller.items[str(self.source)]
        item.update(state="prepared", result={"verified": True, "candidate_path": str(self.source),
                                               "saved_bytes": 1024**3})
        captured = {}

        def cancel_dialog():
            dialog = QApplication.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                QTimer.singleShot(20, cancel_dialog)
                return
            captured.update(title=dialog.windowTitle(), text=dialog.text(),
                            cancel=dialog.button(QMessageBox.StandardButton.Cancel).text(),
                            replace=dialog.button(QMessageBox.StandardButton.Yes).text(),
                            default=dialog.defaultButton())
            captured["cancel_is_default"] = dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.Cancel)
            dialog.button(QMessageBox.StandardButton.Cancel).click()

        QTimer.singleShot(20, cancel_dialog)
        self.controller.commit("replace", [str(self.source)])
        self.assertIn("permanently removed", captured["text"])
        self.assertIn("no original copy", captured["text"])
        self.assertIn("测试视频.mp4", captured["text"])
        self.assertEqual(captured["cancel"], "Cancel")
        self.assertEqual(captured["replace"], "Replace original")
        self.assertTrue(captured["cancel_is_default"])
        self.assertIsNone(self.controller.job)
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
