"""Check rendered frames and shortcuts through the actual Qt controls."""
import time
import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy, QTest
from tests import test_gui as gui


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui.DesktopEditingTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        gui.DesktopEditingTests.tearDownClass.__func__(cls)

    setUp = gui.DesktopEditingTests.setUp
    tearDown = gui.DesktopEditingTests.tearDown

    def wait_for(self, condition, message, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.application.processEvents()
            if condition():
                return
            QTest.qWait(20)
        self.fail(message)

    def seek_frame(self, target):
        self.window.player.pause()
        self.window.player.setPosition(target)
        self.wait_for(lambda: self.window._frame_times is not None
                      and abs(self.window._frame_times[0] / 1000 - target) < 45,
                      "Seek did not display the requested frame")

    def test_drag_displays_new_frames_before_release_and_emits_one_release(self):
        window = self.window
        self.wait_for(lambda: window._frame_times is not None, "First frame was not displayed on load")
        self.assertEqual(window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
        observed = []
        window.video_widget.videoSink().videoFrameChanged.connect(
            lambda frame: observed.append((window.seekbar.isSliderDown(), frame.startTime())) if frame.isValid() else None)
        release = QSignalSpy(window.seekbar.sliderReleased)
        start = QPoint(round(window.seekbar._x_for(3000)), window.seekbar.height() // 2)
        destination = QPoint(round(window.seekbar._x_for(20000)), window.seekbar.height() // 2)
        QTest.mousePress(window.seekbar, Qt.MouseButton.LeftButton, pos=start)
        QTest.qWait(150)
        QTest.mouseMove(window.seekbar, destination, 30)
        self.assertTrue(window.seekbar.isSliderDown())
        target = window.seekbar.value()
        self.assertGreater(target, 19000)
        self.wait_for(lambda: any(held and abs(pts / 1000 - target) < 45 for held, pts in observed),
                      "Dragging did not update the video before mouse release")
        self.assertEqual(release.count(), 0)
        QTest.mouseRelease(window.seekbar, Qt.MouseButton.LeftButton, pos=destination)
        self.assertEqual(release.count(), 1)
        self.assertEqual(window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)

    def test_frame_buttons_display_exact_adjacent_frames_in_both_directions(self):
        window = self.window
        self.seek_frame(4000)
        original = window._frame_times[0]
        for direction, button in ((1, window.next_frame_button), (-1, window.previous_frame_button)):
            before = window._frame_times[0]
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.wait_for(lambda: not window._frame_step_pending and window._frame_times[0] != before,
                          "Frame button did not display an adjacent frame")
            difference = window._frame_times[0] - before
            self.assertAlmostEqual(difference, direction * 1_000_000 / 24, delta=2)
            self.assertEqual(window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
        self.assertEqual(window._frame_times[0], original)
        self.seek_frame(0)
        QTest.mouseClick(window.previous_frame_button, Qt.MouseButton.LeftButton)
        QTest.qWait(100)
        self.assertEqual(window._frame_times[0], 0)

    def test_space_controls_playback_with_list_slider_and_action_buttons_focused(self):
        window = self.window
        self.seek_frame(1000)
        remove = QSignalSpy(window.remove_files_requested)
        refresh = QSignalSpy(window.refresh_requested)
        marks = QSignalSpy(window.marks_changed)
        for focus in (window.file_list, window.seekbar, window.next_frame_button,
                      window.refresh_button, window.remove_files_button, window.mark_in_button):
            focus.setFocus()
            QTest.keyClick(focus, Qt.Key.Key_Space)
            self.wait_for(lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                          "Space did not start playback with " + focus.objectName())
            QTest.keyClick(focus, Qt.Key.Key_Space)
            self.wait_for(lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PausedState,
                          "Space did not pause playback with " + focus.objectName())
        self.assertEqual(remove.count(), 0)
        self.assertEqual(refresh.count(), 0)
        self.assertEqual(marks.count(), 0)
        self.assertEqual(window.range_list.count(), 0)

    def test_space_in_path_editor_stays_text_and_scrub_resumes_playback(self):
        window = self.window
        self.seek_frame(1000)
        window.folder_input.setFocus()
        window.folder_input.selectAll()
        QTest.keyClicks(window.folder_input, "folder name")
        self.assertEqual(window.folder_input.text(), "folder name")
        self.assertEqual(window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
        window.play_button.click()
        self.wait_for(lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState, "Did not play")
        point = QPoint(round(window.seekbar._x_for(12000)), window.seekbar.height() // 2)
        QTest.mousePress(window.seekbar, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
        QTest.mouseRelease(window.seekbar, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(window.player.playbackState(), QMediaPlayer.PlaybackState.PlayingState)


if __name__ == "__main__":
    unittest.main()
