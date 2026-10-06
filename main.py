"""Desktop application entry point and background work coordination."""
from __future__ import annotations

import argparse
import copy
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt, QEvent, QLockFile
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from library import (StateStore, content_hash, default_data_directory, normalize_ranges, path_key,
                     same_source, scan_directory)
from tool_paths import resolve_tools, resource_directory
from version import APP_NAME, VERSION


class JobSignals(QObject):
    progress = Signal(object)
    result = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Job(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = JobSignals()
        self.cancel = threading.Event()

    def run(self):
        try:
            self.signals.result.emit(self.action(self.signals.progress.emit, self.cancel))
        except Exception as error:
            logging.exception("Background operation failed")
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit()


class FingerprintJob(QRunnable):
    def __init__(self, store, item):
        super().__init__()
        self.store, self.snapshot = store, copy.deepcopy(item)
        self.signals = JobSignals()

    def run(self):
        try:
            path = self.snapshot["path"]
            digest = content_hash(path)
            if not self.store.record_fingerprint(path, self.snapshot, digest):
                raise RuntimeError("读取期间视频发生变化，请重新打开目录。")
            self.signals.result.emit(dict(snapshot=self.snapshot, digest=digest))
        except Exception as error:
            logging.exception("Unable to save source fingerprint")
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit()


class Controller(QObject):
    def __init__(self, window, store, tools):
        super().__init__(window)
        self.window = window
        self.store = store
        self.tools = tools
        self.items = {}
        self.folder = ""
        self.job = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.fingerprint_pool = QThreadPool(self)
        self.fingerprint_pool.setMaxThreadCount(1)
        self.fingerprinting = {}
        self._closing = False
        self._job_kind = ""
        window.open_folder_requested.connect(self.open_folder)
        window.refresh_requested.connect(self.refresh_folder)
        window.remove_files_requested.connect(self.remove_files)
        window.restore_files_requested.connect(self.restore_files)
        window.marks_changed.connect(self.save_marks)
        window.prepare_requested.connect(self.prepare)
        window.commit_requested.connect(self.commit)
        window.selection_changed.connect(self.select_item)
        window.cancel_requested.connect(self.cancel_job)
        window.installEventFilter(self)

    def select_item(self, path):
        self.store.set_selection(path)
        item = self.items.get(path)
        if not item or item.get("content_sha256") or not item.get("duration_ms"):
            return
        key = (path, item.get("size_bytes"), item.get("mtime_ns"), item.get("file_id"))
        if key in self.fingerprinting:
            return
        item["fingerprint_pending"] = True
        self.store.save_item(item)
        self.update_window_item(item)
        job = FingerprintJob(self.store, item)
        self.fingerprinting[key] = job
        job.signals.result.connect(self.receive_fingerprint)
        job.signals.failed.connect(self.window.show_error)
        job.signals.finished.connect(lambda: self.fingerprinting.pop(key, None))
        self.fingerprint_pool.start(job)

    def receive_fingerprint(self, result):
        snapshot = result["snapshot"]
        item = self.items.get(snapshot["path"])
        if item and not self.store.is_excluded(self.folder or Path(snapshot["path"]).parent,
                                               snapshot["path"]) and same_source(item, snapshot):
            item.update(content_sha256=result["digest"], fingerprint_pending=False)
            self.update_window_item(item)

    def cancel_job(self):
        if self.job and self._job_kind != "commit":
            self.job.cancel.set()
            self.window.set_progress(0, 0, "正在停止处理，已保存标记…")
        elif self.job:
            self.window.set_progress(0, 0, "正在完成文件操作，请稍候。")

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close and self.job:
            event.ignore()
            if self._job_kind == "commit":
                self.window.set_progress(0, 0, "正在完成文件操作，请稍候再关闭。")
            else:
                self._closing = True
                self.job.cancel.set()
                self.window.set_progress(0, 0, "正在停止处理并保存标记…")
            return True
        return super().eventFilter(watched, event)

    def start_job(self, kind, action, receive):
        if self.job:
            return
        self._job_kind = kind
        self.job = Job(action)
        self.job.signals.progress.connect(self.receive_progress)
        self.job.signals.result.connect(receive)
        self.job.signals.failed.connect(self.window.show_error)
        self.job.signals.finished.connect(self.finish_job)
        self.window.set_busy(True)
        self.pool.start(self.job)

    def finish_job(self):
        for item in self.items.values():
            if item.get("state") in {"preparing", "committing"}:
                item["state"] = "prepared" if item.get("result") else "marked" if item.get("ranges") else "unmarked"
                self.store.save_item(item)
                self.update_window_item(item)
        self.job = None
        self.window.set_busy(False)
        if self._closing:
            self.window.close()

    def receive_progress(self, event):
        if event.get("item"):
            item = event["item"]
            self.items[item["path"]] = item
            self.store.save_item(item)
            self.update_window_item(item)
        if "message" in event:
            self.window.set_progress(event.get("current", 0), event.get("total", 0), event["message"])

    def update_window_item(self, item):
        self.window.update_item(item["path"], **{key: value for key, value in item.items() if key != "path"})

    @staticmethod
    def _listed_path(items, path):
        if not path:
            return None
        key = path_key(path)
        return next((item["path"] for item in items if path_key(item.get("path", "")) == key), None)

    def refresh_folder(self, directory=""):
        directory = str(directory or self.folder or self.store.last_directory).strip()
        if directory:
            self.open_folder(directory)

    def open_folder(self, directory):
        if self.job:
            return
        directory = str(Path(directory).expanduser().resolve())

        def scan(emit, cancel):
            return scan_directory(directory, self.tools, self.store,
                                  lambda current, total, message: emit(dict(current=current, total=total, message=message)),
                                  cancel)

        def receive(items):
            self.folder = directory
            self.items = {item["path"]: item for item in items}
            self.store.set_directory(directory)
            selected = self._listed_path(items, self.window.current_path)
            if selected is None:
                selected = self._listed_path(items, self.store.last_selection)
            self.window.set_library(directory, items, selected)
            self.window.set_removed_count(self.store.removed_count(directory))
            self.window.set_progress(1, 1, f"已读取 {len(items)} 条视频 · 标记自动保存")

        self.start_job("scan", scan, receive)

    def remove_files(self, paths):
        """Exclude selected rows from the list while retaining their source and saved work."""
        if self.job or not self.folder or not paths:
            return

        folder_key = path_key(self.folder)
        old_paths = list(self.items)
        current_path = self.window.current_path
        indexed = {path_key(path): path for path in old_paths}
        targets = []
        for raw_path in paths:
            try:
                key = path_key(raw_path)
                path = indexed.get(key)
                if path and path_key(Path(path).parent) == folder_key:
                    targets.append(path)
            except (OSError, TypeError, ValueError):
                continue
        targets = list(dict.fromkeys(targets))
        if not targets:
            self.window.set_removed_count(self.store.removed_count(self.folder))
            return

        target_keys = {path_key(path) for path in targets}
        self.store.exclude_paths(self.folder, targets)
        remaining = [item for path, item in self.items.items() if path_key(path) not in target_keys]
        current_key = path_key(current_path) if current_path else ""
        selected = self._listed_path(remaining, current_path)
        if selected is None and current_key in target_keys and remaining:
            old_index = next((index for index, path in enumerate(old_paths)
                              if path_key(path) == current_key), 0)
            selected = remaining[min(old_index, len(remaining) - 1)]["path"]

        self.items = {item["path"]: item for item in remaining}
        self.window.set_library(self.folder, remaining, selected)
        self.window.set_removed_count(self.store.removed_count(self.folder))
        self.window.set_progress(1, 1, f"已从列表移除 {len(targets)} 条；原文件和标记均已保留")

    def restore_files(self, directory=""):
        """Restore excluded rows only for the currently displayed directory."""
        if self.job or not self.folder:
            return
        requested = str(directory or self.folder).strip()
        try:
            if path_key(requested) != path_key(self.folder):
                return
        except (OSError, TypeError, ValueError):
            return
        self.store.clear_exclusions(self.folder)
        self.window.set_removed_count(self.store.removed_count(self.folder))
        self.open_folder(self.folder)

    def save_marks(self, path, payload):
        item = self.items.get(path)
        if not item or self.job or not item.get("content_sha256"):
            return
        ranges = normalize_ranges(payload.get("ranges", []), item["duration_ms"])
        if ranges != item.get("ranges", []):
            self.discard_candidate(item)
            item["result"] = None
            item["state"] = "marked" if ranges else "unmarked"
            item["error"] = ""
        item.update(ranges=ranges, draft_start_ms=payload.get("draft_start_ms"),
                    draft_end_ms=payload.get("draft_end_ms"))
        self.store.save_item(item)
        self.update_window_item(item)

    def discard_candidate(self, item):
        result = item.get("result")
        if not result:
            return
        source = Path(item["path"]).resolve()
        roots = [(self.store.directory / "candidates").resolve(),
                 (source.parent / ".片段助手缓存").resolve()]
        media_paths = list(result.get("temporary_media_paths") or [])
        if result.get("candidate_path"):
            media_paths.append(result["candidate_path"])
        for raw in dict.fromkeys(media_paths):
            path = Path(raw).resolve()
            if path == source or not path.is_file() or not any(path.is_relative_to(root) for root in roots):
                continue
            if path == Path(self.window.current_media_path or "").resolve():
                self.window.release_media()
            try:
                path.unlink()
            except OSError:
                logging.warning("Could not remove obsolete candidate %s", path)

    def prepare(self, paths):
        from media_engine import prepare_segments
        targets = [copy.deepcopy(self.items[path]) for path in paths
                   if path in self.items and self.items[path].get("ranges")
                   and self.items[path].get("content_sha256")]
        if not targets or self.job:
            return
        for item in targets:
            self.discard_candidate(item)

        def work(emit, cancel):
            errors = []
            for index, item in enumerate(targets):
                if cancel.is_set():
                    break
                item.update(state="preparing", result=None, error="")
                emit(dict(item=copy.deepcopy(item)))

                def progress(*args):
                    message = str(args[-1]) if args else "处理中…"
                    emit(dict(current=index, total=len(targets), message=f"{index + 1}/{len(targets)} · {item['name']} · {message}"))

                try:
                    result = prepare_segments(item, item["ranges"], Path(item["path"]).parent / ".片段助手缓存",
                                              self.tools, progress=progress, cancel_event=cancel)
                    item.update(state="prepared", result=result)
                    if result.get("source_sha256"):
                        item.update(content_sha256=result["source_sha256"], fingerprint_pending=False)
                    if result.get("prepare_cleanup_errors"):
                        errors.append(f"{item['name']}：裁剪已验证，但部分临时文件未清除。\n" +
                                      "\n".join(result["prepare_cleanup_errors"]))
                except Exception as error:
                    item.update(state="marked", error=str(error))
                    if not cancel.is_set():
                        errors.append(f"{item['name']}：{error}")
                emit(dict(item=copy.deepcopy(item)))
            return {"errors": errors, "cancelled": cancel.is_set()}

        def receive(result):
            self.window.set_progress(1, 1, "已停止，标记已保存。" if result["cancelled"] else "裁剪结果已准备好，可以回看。")
            if result["errors"]:
                self.window.show_error("\n".join(result["errors"]))

        self.start_job("prepare", work, receive)

    def commit(self, mode, paths):
        from media_engine import commit_trim
        targets = [copy.deepcopy(self.items[path]) for path in paths
                   if path in self.items and self.items[path].get("state") == "prepared"
                   and self.items[path].get("result")]
        if not targets or self.job:
            return
        if mode == "replace":
            count = sum(len(item["ranges"]) for item in targets)
            saving = sum(item["result"].get("saved_bytes", 0) for item in targets) / 1024 ** 3
            names = "\n".join(item["name"] for item in targets[:10])
            if len(targets) > 10:
                names += f"\n…共 {len(targets)} 条"
            response = QMessageBox.warning(self.window, "确认替换原片",
                f"即将替换 {len(targets)} 条原片，保留已标记的 {count} 段，可省约 {saving:.2f} GB。\n\n"
                f"{names}\n\n"
                "每条文件会保留原来的文件名。其余画面将永久移除，成功后不留原片副本。\n\n"
                "请确认已回看裁剪结果。现在替换吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if response != QMessageBox.StandardButton.Yes:
                return
        self.window.release_media()

        def work(emit, cancel):
            errors, completed = [], []
            for index, item in enumerate(targets):
                item["state"] = "committing"
                emit(dict(item=copy.deepcopy(item), current=index, total=len(targets), message=f"正在{'替换' if mode == 'replace' else '导出'} · {item['name']}"))
                try:
                    prepared = dict(item["result"], tool_paths=self.tools)
                    receipt = commit_trim(prepared, mode=mode, confirmed=mode == "replace")
                    if mode == "replace":
                        metadata = dict(receipt["source_metadata_after"], content_sha256=receipt["final_sha256"])
                        item = self.store.attach_saved(metadata)
                        item.update(ranges=[], draft_start_ms=None, draft_end_ms=None, state="completed", result=None)
                    else:
                        item.update(state="marked", result=None)
                    item["last_receipt"] = receipt
                    completed.append(receipt)
                except Exception as error:
                    item.update(state="prepared", error=str(error))
                    errors.append(f"{item['name']}：{error}")
                emit(dict(item=copy.deepcopy(item)))
            return {"errors": errors, "completed": completed}

        def receive(result):
            message = f"已{'替换' if mode == 'replace' else '导出'} {len(result['completed'])} 条视频。"
            if mode == "export" and result["completed"]:
                message += f" 保存位置：{Path(result['completed'][0]['final_path']).parent}"
            self.window.set_progress(1, 1, message)
            if self.window.current_path:
                self.window.load_item(self.window.current_path)
            cleanup_errors = [error for receipt in result["completed"] for error in receipt.get("cleanup_errors", [])]
            if cleanup_errors:
                self.window.show_error("裁剪文件已保存，但部分临时文件未清除：\n" + "\n".join(cleanup_errors))
            if result["errors"]:
                self.window.show_error("\n".join(result["errors"]))

        self.start_job("commit", work, receive)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?")
    parser.add_argument("--data-dir")
    parser.add_argument("--verify-desktop", help=argparse.SUPPRESS)
    args = parser.parse_args()
    data_directory = Path(args.data_dir) if args.data_dir else default_data_directory()
    data_directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data_directory / "app.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName("FishingVideoStudio")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setWindowIcon(QIcon(str(resource_directory() / "assets/icon.ico")))
    instance_lock = QLockFile(str(data_directory / "application.lock"))
    if not instance_lock.tryLock(0):
        QMessageBox.information(None, "片段助手", "片段助手已经打开，请使用已有窗口。")
        return
    try:
        tools = resolve_tools()
        from window import TrimWindow
        window = TrimWindow()
        store = StateStore(data_directory)
        controller = Controller(window, store, tools)
        window.show()
        if store.warning:
            QTimer.singleShot(0, lambda: window.show_error(store.warning))
        initial = args.directory or store.last_directory
        if initial and Path(initial).is_dir():
            QTimer.singleShot(0, lambda: controller.open_folder(initial))
        if args.verify_desktop:
            from desktop_check import DesktopCheck
            desktop_check = DesktopCheck(app, controller, window, Path(args.verify_desktop))
            desktop_check.start()
        exit_code = app.exec()
        controller.fingerprint_pool.waitForDone()
        sys.exit(exit_code)
    except Exception as error:
        logging.exception("Unable to launch application")
        QMessageBox.critical(None, "片段助手", str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
