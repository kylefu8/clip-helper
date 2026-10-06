"""Opt-in verification of the frozen application's actual Qt window."""
from __future__ import annotations

import json
import time
import copy
import faulthandler
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, QEvent, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtMultimedia import QAudioBufferOutput
from PySide6.QtWidgets import QApplication, QMessageBox

from library import content_hash, path_key
from i18n import get_language, tr


class DesktopCheck(QObject):
    def __init__(self, app, controller, window, report):
        super().__init__(window)
        self.app, self.controller, self.window, self.report = app, controller, window, report
        report.parent.mkdir(parents=True, exist_ok=True)
        self.trace_file = report.with_suffix(".trace.log").open("w", encoding="utf-8")
        faulthandler.dump_traceback_later(45, repeat=True, file=self.trace_file)
        self.last_checkpoint = None
        self.phase = "scan"
        self.stats = dict(passed=False, frames=0, audio_buffers=0, errors=[])
        range_plan = report.with_suffix(".ranges.json")
        self.profile_plan = json.loads(range_plan.read_text(encoding="utf-8")) if range_plan.is_file() else []
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.started = time.monotonic()
        self.deadline = self.started + (900 if self.profile_plan else 120)
        self.audio = QAudioBufferOutput(self)
        window.player.setAudioBufferOutput(self.audio)
        self.audio.audioBufferReceived.connect(self.audio_received)
        window.video_widget.videoSink().videoFrameChanged.connect(self.frame_received)
        window.player.errorOccurred.connect(lambda error, message: self.stats["errors"].append(message))

    def start(self):
        self.timer.start()

    def audio_received(self, buffer):
        self.stats["audio_buffers"] += int(buffer.isValid())

    def frame_received(self, frame):
        if frame.isValid():
            self.stats["frames"] += 1
            if self.stats["frames"] == 30:
                self.report.parent.mkdir(parents=True, exist_ok=True)
                frame.toImage().scaled(600, 1000, Qt.KeepAspectRatio).save(str(self.report.with_suffix(".jpg")), "JPG", 82)
            if self.phase == "profile_preview" and self.stats["frames"] - self.preview_frames == 30:
                target = self.report.parent / (self.preview_source.stem + "-preview.jpg")
                frame.toImage().scaled(600, 1000, Qt.KeepAspectRatio).save(str(target), "JPG", 82)

    def tick(self):
        try:
            if self.phase != self.last_checkpoint:
                self.last_checkpoint = self.phase
                self.report.with_suffix(".progress.json").write_text(
                    json.dumps(dict(self.stats, phase=self.phase), ensure_ascii=False, indent=2), encoding="utf-8")
            if time.monotonic() > self.deadline:
                raise RuntimeError(f"Desktop check timed out in {self.phase}")
            if self.controller.job:
                return
            if self.phase == "scan":
                if not self.controller.items:
                    return
                if (self.window._frame_times is None
                        or self.window.player.playbackState() == self.window.player.PlaybackState.StoppedState):
                    return
                self.source = Path(self.window.current_path)
                self.fixture = self.source.name == "合成测试视频.mp4" and self.source.parent.parent.name == "package-qa"
                if not self.fixture:
                    self.source_inventory = self.inventory(self.source.parent)
                self.before = content_hash(self.source) if self.fixture else None
                self.initial_ranges = [dict(row) for row in self.controller.items[str(self.source)].get("ranges", [])]
                self.metadata = dict(self.controller.items[str(self.source)])
                self.stats.update(file_count=self.window.file_list.count(), source_path=str(self.source),
                                  window_visible=self.window.isVisible(), window_title=self.window.windowTitle(),
                                  application_version=self.app.applicationVersion(), tool_paths=self.controller.tools)
                if self.fixture:
                    self.check_language_switch()
                self.window.audio_output.setVolume(0.01)
                if self.profile_plan:
                    self.start_profile_check()
                    return
                self.press_space(self.window.play_button)
                if self.window.player.playbackState() != self.window.player.PlaybackState.PlayingState:
                    raise RuntimeError("Space did not start the selected video")
                self.stats["space_play"] = True
                self.phase, self.until = "play", time.monotonic() + 3
            elif self.phase == "play" and time.monotonic() >= self.until:
                self.press_space(self.window.refresh_button)
                if self.window.player.playbackState() != self.window.player.PlaybackState.PausedState:
                    raise RuntimeError("Space on refresh did not pause the current video")
                self.stats["space_pause_on_action_button"] = True
                if self.stats["frames"] < 20 or self.stats["audio_buffers"] < 10 or self.stats["errors"]:
                    raise RuntimeError("Frozen player did not decode valid audio and video")
                if not self.fixture:
                    self.start_reload_check()
                    return
                self.window.seekbar.setValue(4000)
                self.phase = "frame_origin"
            elif self.phase == "frame_origin":
                frame = self.window._frame_times
                if frame is None or not 3999 <= frame[0] / 1000 <= 4001:
                    return
                self.origin_frame = frame[0]
                self.window.next_frame_button.click()
                self.phase = "frame_next"
            elif self.phase == "frame_next":
                frame = self.window._frame_times
                if self.window._frame_step_pending or frame is None or frame[0] == self.origin_frame:
                    return
                if abs(frame[0] - self.origin_frame - 1_000_000 / 30) > 2:
                    raise RuntimeError("Next frame skipped or repeated a fixture frame")
                self.stats["next_frame"] = True
                self.window.previous_frame_button.click()
                self.phase = "frame_previous"
            elif self.phase == "frame_previous":
                if self.window._frame_step_pending or self.window._frame_times[0] != self.origin_frame:
                    return
                self.stats["previous_frame"] = True
                self.mouse_timeline(QEvent.Type.MouseButtonPress, 6000, Qt.MouseButton.LeftButton)
                self.mouse_timeline(QEvent.Type.MouseMove, 9000, Qt.MouseButton.LeftButton)
                self.drag_target = self.window.seekbar.value()
                self.phase = "scrub_held"
            elif self.phase == "scrub_held":
                frame = self.window._frame_times
                if frame is None or abs(frame[0] / 1000 - self.drag_target) > 40:
                    return
                if not self.window.seekbar.isSliderDown():
                    raise RuntimeError("Video did not update while the drag was still held")
                self.stats["drag_frame_before_release"] = True
                self.mouse_timeline(QEvent.Type.MouseButtonRelease, 9000, Qt.MouseButton.NoButton)
                self.window.refresh_button.click()
                self.phase = "refreshed"
            elif self.phase == "refreshed":
                item = self.controller.items.get(str(self.source))
                if not item or item["ranges"] != self.initial_ranges:
                    raise RuntimeError("Refresh lost the source marks")
                self.stats["refresh_preserved_marks"] = True
                self.window.remove_files_button.click()
                if self.window.file_list.count() != 0 or not self.source.exists():
                    raise RuntimeError("List removal failed or removed the source")
                saved = self.controller.store.data["files"][path_key(self.source)]
                if saved["ranges"] != self.initial_ranges:
                    raise RuntimeError("List removal lost the source marks")
                self.window.refresh_button.click()
                self.phase = "removed_refresh"
            elif self.phase == "removed_refresh":
                if self.window.file_list.count() != 0:
                    raise RuntimeError("Refresh re-added manually removed files")
                self.stats["remove_kept_source_and_marks"] = True
                self.window.restore_files_button.click()
                self.phase = "restored"
            elif self.phase == "restored":
                item = self.controller.items.get(str(self.source))
                if not item or item["ranges"] != self.initial_ranges:
                    raise RuntimeError("Restoring an excluded video lost its marks")
                self.stats["restore_preserved_marks"] = True
                self.window.prepare_button.click()
                self.phase = "prepare"
            elif self.phase == "prepare":
                item = self.controller.items[str(self.source)]
                if item.get("state") != "prepared":
                    raise RuntimeError(item.get("error") or "Prepare button did not produce a candidate")
                self.prepared = item["result"]
                self.stats["prepared_ranges"] = self.prepared["actual_ranges"]
                self.check_confirmation()
                self.stats["source_frames"] = self.stats["frames"]
                self.window.preview_result_button.click()
                if not self.window.preview_is_candidate:
                    raise RuntimeError("Candidate preview button did not change media")
                self.window.play_button.click()
                self.phase, self.until = "preview", time.monotonic() + 2
            elif self.phase == "preview" and time.monotonic() >= self.until:
                self.window.player.pause()
                if self.stats["frames"] <= self.stats["source_frames"] or self.stats["errors"]:
                    raise RuntimeError("Frozen candidate did not play")
                self.window.export_button.click()
                self.phase = "export"
            elif self.phase == "export":
                item = self.controller.items[str(self.source)]
                receipt = item.get("last_receipt") or {}
                if not receipt.get("committed"):
                    raise RuntimeError(item.get("error") or "Export button did not save a result")
                self.stats.update(export_path=receipt["final_path"], source_unmodified=content_hash(self.source) == self.before,
                                  exported_hash_matches=content_hash(receipt["final_path"]) == self.prepared["sha256"])
                if not self.stats["source_unmodified"] or not self.stats["exported_hash_matches"]:
                    raise RuntimeError("Frozen export bytes or source preservation failed")
                self.finish(True)
            elif self.phase == "real_reload":
                self.check_reload_step()
            elif self.phase == "profile_prepare":
                self.check_profile_prepare()
            elif self.phase == "profile_preview":
                self.check_profile_preview()
        except Exception as error:
            self.stats["failure"] = str(error)
            self.finish(False)

    @staticmethod
    def inventory(directory):
        return {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in directory.iterdir() if path.is_file()}

    def check_language_switch(self):
        """Switch the real window without reloading its media or saved marks."""
        original = get_language()
        media = self.window.player.source()
        position = self.window.player.position()
        playback = self.window.player.playbackState()
        selected = self.window.file_list.currentItem()
        items = copy.deepcopy(self.controller.items)
        saved = copy.deepcopy(self.controller.store.data)
        draft = (self.window.start_input.text(), self.window.end_input.text())
        for language in ("en" if original == "zh" else "zh", original):
            self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
            if (self.window.player.source() != media or self.window.player.position() != position
                    or self.window.player.playbackState() != playback
                    or self.window.file_list.currentItem() is not selected
                    or self.controller.items != items or self.controller.store.data != saved
                    or (self.window.start_input.text(), self.window.end_input.text()) != draft):
                raise RuntimeError("Language switching changed the media or editing state")
            if self.window.prepare_button.text() != tr("准备已标记的视频（1）"):
                raise RuntimeError("Language switching did not translate the actual controls")
        self.stats["language_switch_preserved_state"] = True
        self.stats["language"] = original

    def check_confirmation(self):
        """Inspect and cancel the real replacement dialog on synthetic media only."""
        self.timer.stop()
        captured = {}

        def inspect_dialog():
            dialog = QApplication.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                QTimer.singleShot(20, inspect_dialog)
                return
            cancel = dialog.button(QMessageBox.StandardButton.Cancel)
            replace = dialog.button(QMessageBox.StandardButton.Yes)
            captured.update(title=dialog.windowTitle(), text=dialog.text(),
                            cancel=cancel.text(), replace=replace.text(),
                            cancel_is_default=dialog.defaultButton() is cancel)
            dialog.grab().save(str(self.report.with_name(self.report.stem + "-confirm.jpg")), "JPG", 82)
            cancel.click()

        QTimer.singleShot(20, inspect_dialog)
        self.window.replace_button.click()
        self.timer.start()
        if (not captured.get("cancel_is_default") or captured.get("cancel") != tr("取消")
                or captured.get("replace") != tr("替换原片") or self.controller.job):
            raise RuntimeError("The translated confirmation did not preserve its cancel behavior")
        if get_language() == "en" and ("permanently removed" not in captured["text"]
                                         or "no original copy" not in captured["text"]):
            raise RuntimeError("The English confirmation omitted the replacement consequences")
        self.stats["replacement_confirmation"] = captured
        self.window.grab().save(str(self.report.with_name(self.report.stem + "-prepared.jpg")), "JPG", 82)

    def start_profile_check(self):
        self.profile_paths = []
        for entry in self.profile_plan:
            source = Path(entry["source_path"]).resolve()
            item = self.controller.items.get(str(source))
            if (source.parent != self.source.parent or not item
                    or item.get("ranges") != entry["ranges"]
                    or item.get("content_sha256") != entry["source_sha256"]):
                raise RuntimeError("Profile check requires saved marks bound to the specified source: " + source.name)
            self.profile_paths.append(str(source))
        if set(self.window._valid_marked_paths()) != set(self.profile_paths):
            raise RuntimeError("Profile check contains unplanned marked videos")
        self.stats["requested_ranges"] = self.profile_plan
        self.window.player.pause()
        self.window.prepare_button.click()
        if not self.controller.job:
            raise RuntimeError("Prepare button did not start the profile check")
        self.phase = "profile_prepare"

    def check_profile_prepare(self):
        prepared = []
        for path in self.profile_paths:
            item = self.controller.items[path]
            result = item.get("result") or {}
            if item.get("state") != "prepared" or not result.get("verified"):
                raise RuntimeError(item.get("error") or "Profile candidate was not verified: " + Path(path).name)
            if result.get("requested_ranges") != item["ranges"]:
                raise RuntimeError("Preparation changed the requested ranges")
            prepared.append(result)
        self.stats.update(prepared=prepared, export_button=self.window.export_button.text(),
                          replace_button=self.window.replace_button.text())
        if not self.window.export_button.isVisible() or not self.window.replace_button.isEnabled():
            raise RuntimeError("Verified results did not enable the desktop actions")
        self.window.grab().save(str(self.report.with_name(self.report.stem + "-window.jpg")), "JPG", 82)
        self.profile_preview_index = 0
        self.stats["candidate_previews"] = []
        self.start_profile_preview()

    def start_profile_preview(self):
        path = self.profile_paths[self.profile_preview_index]
        self.preview_source = Path(path)
        self.window.file_list.setCurrentItem(self.window._rows[path_key(path)])
        self.window.preview_result_button.click()
        if not self.window.preview_is_candidate:
            raise RuntimeError("Profile candidate preview button did not load the result")
        self.preview_frames = self.stats["frames"]
        self.preview_audio = self.stats["audio_buffers"]
        self.until = time.monotonic() + 5
        self.phase = "profile_preview"
        self.window.play_button.click()

    def check_profile_preview(self):
        if time.monotonic() < self.until:
            return
        frames = self.stats["frames"] - self.preview_frames
        audio = self.stats["audio_buffers"] - self.preview_audio
        if frames < 20 or audio < 10 or self.stats["errors"]:
            raise RuntimeError("Profile candidate preview did not decode audio and video: " + self.preview_source.name)
        self.window.player.pause()
        self.stats["candidate_previews"].append(dict(source_path=str(self.preview_source), frames=frames, audio_buffers=audio))
        self.profile_preview_index += 1
        if self.profile_preview_index < len(self.profile_paths):
            self.start_profile_preview()
            return
        self.stats["source_inventory_unchanged"] = self.inventory(self.source.parent) == self.source_inventory
        if not self.stats["source_inventory_unchanged"]:
            raise RuntimeError("Source files changed during the profile check")
        self.finish(True)

    def start_reload_check(self):
        source = str(self.source)
        other = next((path for path in self.controller.items if path != source), source)
        self.reload_steps = []
        for _ in range(3):
            self.reload_steps.extend([("select", other), ("select", source),
                                      ("refresh", None), ("remove", None),
                                      ("refresh", None), ("restore", None),
                                      ("select", source)])
        self.reload_pending = None
        self.stats["reload_steps"] = []
        self.phase = "real_reload"

    def check_reload_step(self):
        source = str(self.source)
        if self.reload_pending is not None:
            action, expected = self.reload_pending
            if (self.window.current_path != expected or self.window._frame_times is None
                    or self.window.player.playbackState() != self.window.player.PlaybackState.PausedState):
                return
            saved = self.controller.store.data["files"].get(path_key(self.source), {})
            if saved.get("ranges", []) != self.initial_ranges:
                raise RuntimeError("Real directory reload changed saved marks")
            if self.controller.store.is_excluded(self.source.parent, self.source):
                if source in self.controller.items or not self.source.exists():
                    raise RuntimeError("Excluded source returned or was deleted")
            elif source not in self.controller.items:
                raise RuntimeError("Existing source disappeared after reload")
            self.stats["reload_steps"].append(action)
            self.reload_pending = None
            return
        if not self.reload_steps:
            self.stats["source_inventory_unchanged"] = self.inventory(self.source.parent) == self.source_inventory
            self.stats["real_reload_preserved_marks"] = True
            if not self.stats["source_inventory_unchanged"] or self.stats["errors"]:
                raise RuntimeError("Real directory changed or playback reported an error")
            self.finish(True)
            return
        action, target = self.reload_steps.pop(0)
        if action == "select":
            self.window.file_list.setCurrentItem(self.window._rows[path_key(target)])
        elif action == "refresh":
            self.window.refresh_button.click()
        elif action == "remove":
            self.window.remove_files_button.click()
            if source in self.controller.items or not self.source.exists():
                raise RuntimeError("Removing current source failed or deleted the video")
        elif action == "restore":
            self.window.restore_files_button.click()
        self.reload_pending = (action, target or self.window.current_path)

    def press_space(self, widget):
        before = self.window.player.playbackState().name
        # Background launch can leave a frozen window inactive. Establish Qt's
        # active window before injecting the same key events a focused user sends.
        self.window.activateWindow()
        QApplication.setActiveWindow(self.window)
        widget.setFocus()
        focus = QApplication.focusWidget()
        active = QApplication.activeWindow()
        if focus is not widget or active is not self.window:
            raise RuntimeError("Desktop check could not focus " + widget.objectName())
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(widget, QKeyEvent(kind, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier))
        self.stats.setdefault("space_events", []).append(dict(
            target=widget.objectName(), focus=focus.objectName() if focus else None,
            active_window=active.objectName() if active else None,
            before=before, after=self.window.player.playbackState().name,
        ))

    def mouse_timeline(self, kind, position, buttons):
        bar = self.window.seekbar
        point = QPointF(bar._x_for(position), bar.height() / 2)
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        QApplication.sendEvent(bar, QMouseEvent(kind, point, button, buttons, Qt.KeyboardModifier.NoModifier))

    def finish(self, passed):
        self.timer.stop()
        faulthandler.cancel_dump_traceback_later()
        self.trace_file.close()
        self.stats.update(passed=passed, phase=self.phase, elapsed_seconds=time.monotonic() - self.started)
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.window.grab().save(str(self.report.with_name(self.report.stem + "-window.jpg")), "JPG", 82)
        self.report.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2), encoding="utf-8")
        self.window.release_media()
        self.app.quit()
