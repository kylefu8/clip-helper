"""Native PySide6 interface for manually marking several keep ranges per source clip."""
from __future__ import annotations

import os
import math
from typing import Any
from version import APP_NAME, VERSION
from i18n import (get_language, localized_button, localized_label, retranslate,
                  set_language, set_ui_text, tr)

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QStackedLayout, QVBoxLayout, QWidget,
)

try:
    from .widgets import RangeSlider, VideoItemDelegate
except ImportError:
    from widgets import RangeSlider, VideoItemDelegate

STATES = {"unmarked": "未标记", "marked": "已标记", "preparing": "处理中…", "prepared": "候选就绪", "complete": "已完成", "completed": "已完成", "committing": "处理中…", "error": "有问题"}
STYLE = """
QMainWindow, QWidget { background:#10151c; color:#e7edf5; font-family:'Microsoft YaHei UI','Microsoft YaHei',sans-serif; font-size:10pt; }
QFrame#panel { background:#171e27; border:1px solid #27313e; border-radius:14px; }
QFrame#videoStage { background:#06080b; border:1px solid #222c38; border-radius:12px; }
QLabel#mutedLabel { color:#8b9bae; }
QLabel#windowTitle { color:#f4f7fb; font-size:20pt; font-weight:700; }
QLabel#sectionTitle { color:#eaf0f8; font-size:12pt; font-weight:650; }
QLabel#stageHint { background:transparent; color:#8190a1; font-size:12pt; }
QLineEdit,QComboBox { background:#111821; border:1px solid #334153; border-radius:8px; padding:8px 10px; selection-background-color:#2d796e; }
QLineEdit:focus,QComboBox:focus { border-color:#55cdb8; }
QLineEdit[invalid='true'] { border-color:#ed7777; }
QComboBox::drop-down { border:0; width:24px; }
QComboBox QAbstractItemView { background:#18212b; color:#e7edf5; selection-background-color:#285b56; border:1px solid #3a4b5d; }
QPushButton { color:#e6edf5; background:#26313e; border:1px solid #364455; border-radius:8px; padding:8px 11px; min-height:18px; }
QPushButton:hover { background:#303e4d; border-color:#53677d; }
QPushButton:pressed { background:#1c2732; }
QPushButton:disabled { color:#617080; background:#1a212a; border-color:#252f3b; }
QPushButton[primary='true'] { background:#25695f; border-color:#338579; color:white; font-weight:650; }
QPushButton[primary='true']:hover { background:#2d7b70; }
QPushButton[danger='true'] { background:#71373b; border-color:#95494b; color:white; font-weight:650; }
QPushButton[mark='true'] { background:#203c39; border-color:#32645c; color:#baf1e7; font-weight:650; }
QListWidget { background:#11171f; border:1px solid #26313e; border-radius:10px; padding:4px; outline:0; }
QListWidget::item { border:0; padding:0; }
QListWidget#range_list { background:#11171f; border:1px solid #26313e; border-radius:9px; padding:3px; }
QSlider#volumeSlider::groove:horizontal { height:4px; background:#34404d; border-radius:2px; }
QSlider#volumeSlider::sub-page:horizontal { background:#49bca9; border-radius:2px; }
QSlider#volumeSlider::handle:horizontal { background:#e6f3f1; border:0; width:12px; margin:-5px 0; border-radius:6px; }
QProgressBar { background:#242e39; border:0; border-radius:4px; height:7px; text-align:center; }
QProgressBar::chunk { background:#4cbda9; border-radius:4px; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { background:transparent; width:9px; margin:4px 1px; }
QScrollBar::handle:vertical { background:#394757; min-height:22px; border-radius:4px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
"""


def canonical(path: str | None) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path))) if path else ""


def fmt(ms: int | None) -> str:
    if ms is None:
        return ""
    h, rem = divmod(max(0, int(ms)), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}" if h else f"{m:02d}:{s:02d}.{milli:03d}"


def parse_timecode(text: str) -> int | None:
    """Parse mm:ss.fff or hh:mm:ss.fff. Empty text clears that draft marker."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) not in (2, 3) or any(not part for part in parts):
        raise ValueError("格式用 分:秒.毫秒 或 时:分:秒.毫秒")
    sec_parts = parts[-1].split(".", 1)
    sec_text = sec_parts[0]
    fraction = sec_parts[1] if len(sec_parts) == 2 else ""
    if not sec_text.isdigit() or (fraction and not fraction.isdigit()) or len(fraction) > 3:
        raise ValueError("请填写有效的时间，毫秒最多三位")
    sec = int(sec_text)
    if sec >= 60:
        raise ValueError("秒数必须小于 60")
    if len(parts) == 2:
        if not parts[0].isdigit():
            raise ValueError("分钟只能填写数字")
        h, m = 0, int(parts[0])
    else:
        if not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("小时和分钟只能填写数字")
        h, m = int(parts[0]), int(parts[1])
        if m >= 60:
            raise ValueError("小时格式中的分钟必须小于 60")
    return ((h * 60 + m) * 60 + sec) * 1000 + (int(fraction.ljust(3, "0")) if fraction else 0)


def human_size(size: int | None) -> str:
    size = max(0, int(size or 0))
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def item_details(item: dict[str, Any]) -> str:
    parts = []
    if item.get("duration_ms"):
        parts.append(fmt(item["duration_ms"]))
    w, h = int(item.get("width") or 0), int(item.get("height") or 0)
    if w and h:
        if abs(int(item.get("rotation") or 0)) % 180 == 90:
            w, h = h, w
        parts.append(f"{w}×{h}")
    if item.get("frame_rate"):
        parts.append(f"{float(item['frame_rate']):.2f} fps")
    parts.append(human_size(item.get("size_bytes")))
    return "  ·  ".join(parts)


class TrimWindow(QMainWindow):
    """Single-window, local-only UI; filesystem/media work stays in the controller."""

    open_folder_requested = Signal(str)
    refresh_requested = Signal(str)
    remove_files_requested = Signal(list)
    restore_files_requested = Signal(str)
    selection_changed = Signal(str)
    marks_changed = Signal(str, object)
    prepare_requested = Signal(list)
    commit_requested = Signal(str, list)
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        set_ui_text(self, f"{APP_NAME} v{VERSION}", setter="setWindowTitle")
        self.setObjectName("trimWindow")
        self.setMinimumSize(1250, 760)
        self.resize(1540, 920)
        self.setStyleSheet(STYLE)
        self._items: dict[str, dict[str, Any]] = {}
        self._path_lookup: dict[str, str] = {}
        self._rows: dict[str, QListWidgetItem] = {}
        self._candidate_lookup: dict[str, str] = {}
        self._folder = ""
        self._selected_path: str | None = None
        self._loaded_path: str | None = None
        self._preview_candidate = False
        self._media_released = False
        self._range_index: int | None = None
        self._draft_start: int | None = None
        self._draft_end: int | None = None
        self._duration_ms = 0
        self._fallback_duration_ms = 0
        self._busy = False
        self._playing_segment = False
        self._removed_count = 0
        self._scrub_was_playing = False
        self._scrub_target = None
        self._frame_times = None
        self._frame_step_pending = False
        self._frame_step_target = None
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setInterval(40)
        self._scrub_timer.timeout.connect(self._flush_scrub)
        self._frame_step_timer = QTimer(self)
        self._frame_step_timer.setSingleShot(True)
        self._frame_step_timer.setInterval(1000)
        self._frame_step_timer.timeout.connect(self._finish_frame_step)

        self.player = QMediaPlayer(self)
        self.player.setObjectName("player")
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player.setAudioOutput(self.audio_output)
        self._build_ui()
        self._wire()
        self._shortcuts()
        self._refresh_actions()

    @property
    def current_path(self) -> str | None:
        return self._selected_path

    @property
    def current_media_path(self) -> str | None:
        return self._loaded_path

    @property
    def preview_is_candidate(self) -> bool:
        return self._preview_candidate

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 15, 18, 14)
        root_layout.setSpacing(12)
        header = QHBoxLayout()
        head = QVBoxLayout()
        title = localized_label(f"{APP_NAME} v{VERSION}")
        title.setObjectName("windowTitle")
        subtitle = localized_label("手动选段 · 先预览结果 · 再决定如何保存")
        subtitle.setObjectName("mutedLabel")
        head.addWidget(title)
        head.addWidget(subtitle)
        header.addLayout(head, 1)
        local = localized_label("本机预览与处理")
        local.setObjectName("mutedLabel")
        language_label = localized_label("界面语言")
        language_label.setObjectName("mutedLabel")
        header.addWidget(language_label)
        self.language_combo = QComboBox()
        self.language_combo.setObjectName("language_combo")
        self.language_combo.addItem("中文", "zh")
        self.language_combo.addItem("English", "en")
        self.language_combo.setCurrentIndex(self.language_combo.findData(get_language()))
        self.language_combo.setFixedWidth(125)
        self.language_combo.currentIndexChanged.connect(self._change_language)
        header.addWidget(self.language_combo)
        header.addWidget(local, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root_layout.addLayout(header)
        columns = QHBoxLayout()
        columns.setSpacing(12)
        root_layout.addLayout(columns, 1)
        self._build_library(columns)
        self._build_player(columns)
        self._build_ranges(columns)
        footer = QHBoxLayout()
        self.status_label = localized_label("选择一个目录开始")
        self.status_label.setObjectName("mutedLabel")
        footer.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(180)
        self.progress_bar.setVisible(False)
        footer.addWidget(self.progress_bar)
        self.cancel_button = localized_button("取消")
        self.cancel_button.setVisible(False)
        self.cancel_button.setFixedWidth(76)
        footer.addWidget(self.cancel_button)
        root_layout.addLayout(footer)

    def _change_language(self, index):
        if self._busy:
            self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(self.language_combo.findData(get_language()))
            self.language_combo.blockSignals(False)
            return
        set_language(self.language_combo.itemData(index))
        retranslate(self)
        self.file_list.viewport().update()

    def _panel(self, minimum: int, maximum: int | None = None) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setMinimumWidth(minimum)
        if maximum:
            panel.setMaximumWidth(maximum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(9)
        return panel, layout

    def _build_library(self, columns):
        panel, layout = self._panel(275, 318)
        row = QHBoxLayout()
        label = localized_label("视频目录")
        label.setObjectName("sectionTitle")
        self.file_count_label = localized_label("0 条")
        self.file_count_label.setObjectName("mutedLabel")
        row.addWidget(label)
        row.addStretch(1)
        row.addWidget(self.file_count_label)
        layout.addLayout(row)
        self.folder_input = QLineEdit()
        self.folder_input.setObjectName("folder_input")
        set_ui_text(self.folder_input, "粘贴本机目录路径", setter="setPlaceholderText")
        self.folder_input.setClearButtonEnabled(True)
        layout.addWidget(self.folder_input)
        buttons = QHBoxLayout()
        self.choose_folder_button = localized_button("选择目录")
        self.choose_folder_button.setObjectName("choose_folder_button")
        self.refresh_button = localized_button("刷新")
        self.refresh_button.setObjectName("refresh_button")
        self.refresh_button.setProperty("primary", True)
        set_ui_text(self.refresh_button, "重新扫描目录，保留仍存在文件的标记", setter="setToolTip")
        self.open_folder_button = self.refresh_button
        buttons.addWidget(self.choose_folder_button)
        buttons.addWidget(self.open_folder_button)
        layout.addLayout(buttons)
        self.file_list = QListWidget()
        self.file_list.setObjectName("file_list")
        self.file_list.setItemDelegate(VideoItemDelegate(self.file_list))
        self.file_list.setUniformItemSizes(True)
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.setSpacing(1)
        self.file_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.file_list, 1)
        list_actions = QHBoxLayout()
        self.remove_files_button = localized_button("移除所选")
        self.remove_files_button.setObjectName("remove_files_button")
        set_ui_text(self.remove_files_button, "只从列表移除，视频文件和已保存标记保留", setter="setToolTip")
        self.restore_files_button = localized_button("恢复移除项")
        self.restore_files_button.setObjectName("restore_files_button")
        list_actions.addWidget(self.remove_files_button)
        list_actions.addWidget(self.restore_files_button)
        layout.addLayout(list_actions)
        self.library_hint = localized_label("选择目录后自动列出视频；移除只影响此列表。")
        self.library_hint.setObjectName("mutedLabel")
        self.library_hint.setWordWrap(True)
        layout.addWidget(self.library_hint)
        columns.addWidget(panel, 0)

    def _build_player(self, columns):
        panel, layout = self._panel(420)
        layout.setContentsMargins(15, 13, 15, 13)
        media_head = QHBoxLayout()
        names = QVBoxLayout()
        self.media_title_label = localized_label("尚未选择视频")
        self.media_title_label.setObjectName("sectionTitle")
        self.media_details_label = localized_label("选择左侧视频后可预览原片")
        self.media_details_label.setObjectName("mutedLabel")
        names.addWidget(self.media_title_label)
        names.addWidget(self.media_details_label)
        media_head.addLayout(names, 1)
        self.preview_source_button = localized_button("看原片")
        self.preview_source_button.setVisible(False)
        self.preview_result_button = localized_button("看裁剪结果")
        self.preview_result_button.setObjectName("preview_result_button")
        self.preview_result_button.setVisible(False)
        media_head.addWidget(self.preview_source_button)
        media_head.addWidget(self.preview_result_button)
        layout.addLayout(media_head)

        self.video_stage = QFrame()
        self.video_stage.setObjectName("videoStage")
        self.video_stage.setMinimumHeight(340)
        self.video_stage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        stack = QStackedLayout(self.video_stage)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.video_widget = QVideoWidget(self.video_stage)
        self.video_widget.setObjectName("video_widget")
        self.video_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        stack.addWidget(self.video_widget)
        self.stage_hint = localized_label("选择左侧视频开始预览", self.video_stage)
        self.stage_hint.setObjectName("stageHint")
        self.stage_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stage_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        stack.addWidget(self.stage_hint)
        self.player.setVideoOutput(self.video_widget)
        layout.addWidget(self.video_stage, 1)
        self.player_error_label = localized_label("")
        self.player_error_label.setWordWrap(True)
        self.player_error_label.setStyleSheet("color:#ff9898;")
        self.player_error_label.setVisible(False)
        layout.addWidget(self.player_error_label)

        time_row = QHBoxLayout()
        self.position_label = localized_label("00:00.000")
        self.position_label.setStyleSheet("font-weight:650;")
        self.duration_label = localized_label("/ 00:00.000")
        self.duration_label.setObjectName("mutedLabel")
        time_row.addWidget(self.position_label)
        time_row.addWidget(self.duration_label)
        time_row.addStretch(1)
        time_row.addWidget(localized_label("音量"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("volumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(92)
        time_row.addWidget(self.volume_slider)
        layout.addLayout(time_row)
        self.seekbar = RangeSlider()
        self.seekbar.setObjectName("seekbar")
        set_ui_text(self.seekbar, "点击或拖动跳转；色带表示保留片段", setter="setToolTip")
        layout.addWidget(self.seekbar)

        transport = QHBoxLayout()
        self.back_button = localized_button("−5 秒")
        self.play_button = localized_button("播放")
        self.play_button.setObjectName("play_button")
        self.play_button.setProperty("primary", True)
        self.play_button.setMinimumWidth(82)
        self.previous_frame_button = localized_button("上一帧")
        self.previous_frame_button.setObjectName("previous_frame_button")
        self.next_frame_button = localized_button("下一帧")
        self.next_frame_button.setObjectName("next_frame_button")
        self.forward_button = localized_button("+5 秒")
        self.speed_select = QComboBox()
        self.speed_select.setObjectName("playbackSpeed")
        for rate in (0.25, 0.5, 1.0, 1.5, 2.0, 4.0):
            self.speed_select.addItem(f"{rate:g}×", rate)
        self.speed_select.setCurrentIndex(2)
        self.speed_select.setFixedWidth(78)
        transport.addWidget(self.back_button)
        transport.addWidget(self.previous_frame_button)
        transport.addWidget(self.play_button)
        transport.addWidget(self.next_frame_button)
        transport.addWidget(self.forward_button)
        transport.addWidget(self.speed_select)
        transport.addStretch(1)
        layout.addLayout(transport)
        columns.addWidget(panel, 1)

    def _build_ranges(self, columns):
        panel, layout = self._panel(310, 360)
        title = localized_label("保留片段")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        help_label = localized_label("I / O 设置当前片段起止。每个视频可添加多个片段，最终按原片时间顺序拼接。")
        help_label.setObjectName("mutedLabel")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        start_row = QHBoxLayout()
        start_row.addWidget(localized_label("开始"))
        self.start_input = QLineEdit()
        self.start_input.setObjectName("start_input")
        set_ui_text(self.start_input, "分:秒.毫秒", setter="setPlaceholderText")
        self.start_input.setClearButtonEnabled(True)
        start_row.addWidget(self.start_input, 1)
        self.mark_in_button = localized_button("标开始 I")
        self.mark_in_button.setObjectName("mark_in_button")
        self.mark_in_button.setProperty("mark", True)
        start_row.addWidget(self.mark_in_button)
        layout.addLayout(start_row)
        end_row = QHBoxLayout()
        end_row.addWidget(localized_label("结束"))
        self.end_input = QLineEdit()
        self.end_input.setObjectName("end_input")
        set_ui_text(self.end_input, "分:秒.毫秒", setter="setPlaceholderText")
        self.end_input.setClearButtonEnabled(True)
        end_row.addWidget(self.end_input, 1)
        self.mark_out_button = localized_button("标结束 O")
        self.mark_out_button.setObjectName("mark_out_button")
        self.mark_out_button.setProperty("mark", True)
        end_row.addWidget(self.mark_out_button)
        layout.addLayout(end_row)
        self.draft_status = localized_label("尚未标记当前片段")
        self.draft_status.setWordWrap(True)
        self.draft_status.setStyleSheet("color:#94a4b5;")
        layout.addWidget(self.draft_status)

        draft_buttons = QHBoxLayout()
        self.new_range_button = localized_button("新建片段")
        self.add_range_button = localized_button("添加保留片段")
        self.add_range_button.setProperty("primary", True)
        draft_buttons.addWidget(self.new_range_button)
        draft_buttons.addWidget(self.add_range_button, 1)
        layout.addLayout(draft_buttons)

        range_title_row = QHBoxLayout()
        range_title_row.addWidget(localized_label("已添加"))
        self.range_count_label = localized_label("0 段")
        self.range_count_label.setObjectName("mutedLabel")
        range_title_row.addStretch(1)
        range_title_row.addWidget(self.range_count_label)
        layout.addLayout(range_title_row)
        self.range_list = QListWidget()
        self.range_list.setObjectName("range_list")
        self.range_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.range_list.setMinimumHeight(108)
        self.range_list.setMaximumHeight(210)
        layout.addWidget(self.range_list, 1)

        edit_buttons = QHBoxLayout()
        self.play_segment_button = localized_button("回看当前片段")
        self.update_range_button = localized_button("更新所选")
        self.delete_range_button = localized_button("删除所选")
        edit_buttons.addWidget(self.play_segment_button, 1)
        edit_buttons.addWidget(self.update_range_button)
        edit_buttons.addWidget(self.delete_range_button)
        layout.addLayout(edit_buttons)
        self.range_summary = localized_label("准备后会显示实际拼接范围和节省空间")
        self.range_summary.setWordWrap(True)
        self.range_summary.setObjectName("mutedLabel")
        layout.addWidget(self.range_summary)
        self.keyframe_note = localized_label("无重编码裁剪可能从起点前最近的关键画面开始，实际范围会在准备结果中显示。")
        self.keyframe_note.setWordWrap(True)
        self.keyframe_note.setStyleSheet("color:#c0a87f; background:#29251e; border-radius:8px; padding:8px;")
        layout.addWidget(self.keyframe_note)
        self.prepare_button = localized_button("准备已标记的视频")
        self.prepare_button.setObjectName("prepare_button")
        self.prepare_button.setProperty("primary", True)
        self.prepare_button.setMinimumHeight(40)
        set_ui_text(self.prepare_button, "生成并验证临时结果；此步骤不会改动原片", setter="setToolTip")
        layout.addWidget(self.prepare_button)
        self.export_button = localized_button("保留为新文件")
        self.export_button.setObjectName("export_button")
        self.export_button.setVisible(False)
        layout.addWidget(self.export_button)
        self.replace_button = localized_button("确认替换原片")
        self.replace_button.setObjectName("replace_button")
        self.replace_button.setProperty("danger", True)
        self.replace_button.setVisible(False)
        layout.addWidget(self.replace_button)
        self.feedback_label = localized_label("")
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setStyleSheet("color:#ffb2a8; background:#352326; border-radius:8px; padding:8px;")
        self.feedback_label.setVisible(False)
        layout.addWidget(self.feedback_label)
        columns.addWidget(panel, 0)

    def _wire(self):
        self.refresh_button.clicked.connect(self._refresh_folder)
        self.choose_folder_button.clicked.connect(self._choose_folder)
        self.folder_input.returnPressed.connect(self._open_folder)
        self.file_list.currentItemChanged.connect(self._file_selected)
        self.file_list.itemSelectionChanged.connect(self._refresh_actions)
        self.file_list.customContextMenuRequested.connect(self._library_context_menu)
        self.remove_files_button.clicked.connect(self._remove_selected_files)
        self.restore_files_button.clicked.connect(self._restore_files)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._media_error)
        self.seekbar.valueChanged.connect(self._seekbar_changed)
        self.seekbar.sliderPressed.connect(self._scrub_started)
        self.seekbar.sliderReleased.connect(self._scrub_finished)
        self.video_widget.videoSink().videoFrameChanged.connect(self._video_frame_changed)
        self.play_button.clicked.connect(self._toggle_playback)
        self.previous_frame_button.clicked.connect(lambda: self._step_frame(-1))
        self.next_frame_button.clicked.connect(lambda: self._step_frame(1))
        self.back_button.clicked.connect(lambda: self._seek(-5000))
        self.forward_button.clicked.connect(lambda: self._seek(5000))
        self.volume_slider.valueChanged.connect(lambda v: self.audio_output.setVolume(v / 100.0))
        self.speed_select.currentIndexChanged.connect(self._speed_changed)
        self.mark_in_button.clicked.connect(lambda: self._mark("start"))
        self.mark_out_button.clicked.connect(lambda: self._mark("end"))
        self.start_input.editingFinished.connect(lambda: self._edit_draft("start"))
        self.end_input.editingFinished.connect(lambda: self._edit_draft("end"))
        self.new_range_button.clicked.connect(self._new_range)
        self.add_range_button.clicked.connect(self._add_range)
        self.range_list.currentItemChanged.connect(self._range_selected)
        self.play_segment_button.clicked.connect(self._play_draft)
        self.update_range_button.clicked.connect(self._update_range)
        self.delete_range_button.clicked.connect(self._delete_range)
        self.prepare_button.clicked.connect(self._prepare)
        self.preview_result_button.clicked.connect(self._preview_result)
        self.preview_source_button.clicked.connect(self._preview_source)
        self.export_button.clicked.connect(lambda: self._commit("export"))
        self.replace_button.clicked.connect(lambda: self._commit("replace"))
        self.cancel_button.clicked.connect(self.cancel_requested.emit)

    def _shortcuts(self):
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) or QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            return super().eventFilter(watched, event)
        focus = QApplication.focusWidget()
        if focus is None or not (focus is self or self.isAncestorOf(focus)):
            return super().eventFilter(watched, event)
        if isinstance(focus, (QLineEdit, QAbstractSpinBox)):
            return False
        if event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return super().eventFilter(watched, event)
        # Space always controls this window's current media, including when
        # a button or the timeline has focus. Never let it click a focused
        # remove/replace/mark button as a side effect.
        if event.key() == Qt.Key.Key_Space:
            if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self._toggle_playback()
            event.accept()
            return True
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        if focus is self.file_list and event.key() == Qt.Key.Key_Delete:
            self._remove_selected_files()
            event.accept()
            return True
        if isinstance(focus, (QComboBox, QSlider, QAbstractButton)):
            return False
        actions = {
            Qt.Key.Key_Space: self._toggle_playback,
            Qt.Key.Key_Left: lambda: self._seek(-5000),
            Qt.Key.Key_Right: lambda: self._seek(5000),
            Qt.Key.Key_I: lambda: self._mark("start"),
            Qt.Key.Key_O: lambda: self._mark("end"),
        }
        action = actions.get(event.key())
        if action is None:
            return super().eventFilter(watched, event)
        action()
        event.accept()
        return True

    def _open_folder(self):
        if self._busy:
            return
        folder = self.folder_input.text().strip().strip('"')
        if not folder:
            self.show_error("请先选择或输入视频目录。")
            return
        self._clear_error()
        set_ui_text(self.status_label, "正在读取目录…")
        self.open_folder_requested.emit(folder)

    def _choose_folder(self):
        if self._busy:
            return
        path = QFileDialog.getExistingDirectory(self, tr("选择视频目录"), self.folder_input.text() or self._folder)
        if path:
            set_ui_text(self.folder_input, path)
            self._open_folder()

    def _refresh_folder(self):
        if self._busy:
            return
        folder = self.folder_input.text().strip().strip('"') or self._folder
        if not folder:
            self.show_error("请先选择视频目录。")
            return
        self._clear_error()
        self.refresh_requested.emit(folder)

    def _remove_selected_files(self):
        if not self._busy:
            paths = [str(row.data(Qt.ItemDataRole.UserRole)) for row in self.file_list.selectedItems()]
            if paths:
                self.remove_files_requested.emit(paths)

    def _restore_files(self):
        if not self._busy and self._folder and self._removed_count:
            self.restore_files_requested.emit(self._folder)

    def set_removed_count(self, count):
        self._removed_count = max(0, int(count))
        set_ui_text(self.restore_files_button, f"恢复移除项（{count}）" if count else "恢复移除项")
        self._refresh_actions()

    def _library_context_menu(self, point):
        if self._busy:
            return
        row = self.file_list.itemAt(point)
        if row is not None and not row.isSelected():
            self.file_list.setCurrentItem(row)
        menu = QMenu(self)
        remove = menu.addAction(tr("从列表移除所选视频"))
        remove.setEnabled(bool(self.file_list.selectedItems()))
        remove.triggered.connect(self._remove_selected_files)
        restore = menu.addAction(tr("恢复已移除视频"))
        restore.setEnabled(bool(self._removed_count))
        restore.triggered.connect(self._restore_files)
        menu.exec(self.file_list.viewport().mapToGlobal(point))

    def set_library(self, folder: str, items: list[dict[str, Any]], selected_path: str | None = None):
        self._folder = folder or ""
        set_ui_text(self.folder_input, self._folder)
        self._items.clear()
        self._path_lookup.clear()
        self._rows.clear()
        self._candidate_lookup.clear()
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for original in items or []:
            item = dict(original)
            path = str(item.get("path") or "")
            if not path:
                continue
            item["path"] = path
            item.setdefault("name", os.path.basename(path))
            item.setdefault("ranges", [])
            item.setdefault("draft_start_ms", None)
            item.setdefault("draft_end_ms", None)
            item.setdefault("state", "unmarked")
            item.setdefault("result", None)
            self._items[path] = item
            key = canonical(path)
            self._path_lookup[key] = path
            result = item.get("result") or {}
            if result.get("candidate_path"):
                self._candidate_lookup[canonical(result["candidate_path"])] = path
            row = QListWidgetItem()
            row.setData(Qt.ItemDataRole.UserRole, path)
            self.file_list.addItem(row)
            self._rows[key] = row
            self._refresh_row(path)
        self.file_list.blockSignals(False)
        set_ui_text(self.file_count_label, f"{len(self._items)} 条")
        if not self._items:
            self._selected_path = None
            self.release_media()
            set_ui_text(self.media_title_label, "尚未选择视频")
            set_ui_text(self.media_details_label, "选择左侧视频后可预览原片")
            set_ui_text(self.library_hint, "目录中没有可播放的视频。可选择其他目录。")
            self._refresh_ranges(None)
            set_ui_text(self.status_label, "目录中没有可播放的视频")
            self._refresh_actions()
            return
        set_ui_text(self.library_hint, "单击视频预览；右键或按 Delete 从列表移除。文件和标记仍保留。")
        path = self._source_path(selected_path) if selected_path else None
        path = path or next(iter(self._items))
        row = self._rows[canonical(path)]
        self.file_list.blockSignals(True)
        self.file_list.setCurrentItem(row)
        self.file_list.blockSignals(False)
        self._select_source(path)
        set_ui_text(self.status_label, f"已读取 {len(self._items)} 条视频")
        self._clear_error()

    def update_item(self, path: str, **changes: Any):
        source = self._source_path(path)
        if source is None:
            return
        item = self._items[source]
        old_ranges = self._ranges(item)
        if "result" in changes:
            self._candidate_lookup = {key: value for key, value in self._candidate_lookup.items() if value != source}
        item.update(changes)
        result = item.get("result") or {}
        if result.get("candidate_path"):
            self._candidate_lookup[canonical(result["candidate_path"])] = source
        if source == self._selected_path and "ranges" in changes:
            selected = old_ranges[self._range_index] if self._range_index is not None and self._range_index < len(old_ranges) else None
            normalized = self._ranges(item)
            self._range_index = None
            if selected and normalized:
                match = next((i for i, part in enumerate(normalized)
                              if max(selected["start_ms"], part["start_ms"]) < min(selected["end_ms"], part["end_ms"])
                              or selected == part), None)
                self._range_index = match
            self._render_range_list()
        self._refresh_row(source)
        if source == self._selected_path:
            if not self._preview_candidate:
                self._set_draft(item.get("draft_start_ms"), item.get("draft_end_ms"), update_item=False)
            self._refresh_media_details()
            self._refresh_summary()
            self._refresh_preview_buttons()
        self._refresh_actions()

    def set_busy(self, busy: bool, message: str = ""):
        self._busy = bool(busy)
        self.folder_input.setEnabled(not self._busy)
        self.open_folder_button.setEnabled(not self._busy)
        self.choose_folder_button.setEnabled(not self._busy)
        self._mark_controls(not self._busy and not self._preview_candidate)
        self.cancel_button.setVisible(self._busy)
        self.progress_bar.setVisible(self._busy)
        if self._busy:
            self.progress_bar.setRange(0, 0)
            set_ui_text(self.status_label, message or "正在处理…")
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            if message:
                set_ui_text(self.status_label, message)
        self._refresh_actions()

    def set_progress(self, done: int, total: int, message: str):
        if total > 0:
            self.progress_bar.setRange(0, int(total))
            self.progress_bar.setValue(max(0, min(int(done), int(total))))
        else:
            self.progress_bar.setRange(0, 0)
        if message:
            set_ui_text(self.status_label, message)

    def show_error(self, message: str):
        set_ui_text(self.feedback_label, message or "操作未完成。")
        self.feedback_label.setVisible(True)
        set_ui_text(self.status_label, "操作未完成，请查看提示")

    def load_item(self, path: str):
        if not path:
            self.release_media()
            return
        key = canonical(path)
        source = self._candidate_lookup.get(key)
        candidate = source is not None
        source = source or self._source_path(path)
        item = self._items.get(source or "")
        if source and item:
            self._selected_path = source
            row = self._rows.get(canonical(source))
            if row is not None and self.file_list.currentItem() is not row:
                self.file_list.blockSignals(True)
                self.file_list.setCurrentItem(row)
                self.file_list.blockSignals(False)
        elif not candidate:
            self._selected_path = path
        self._preview_candidate = candidate
        self._media_released = False
        self._loaded_path = path
        self._playing_segment = False
        self._reset_seeking()
        self._clear_player_error()
        self.player.stop()
        self._fallback_duration_ms = self._media_duration(item, candidate)
        self._duration_ms = self._fallback_duration_ms
        self.seekbar.set_duration(self._duration_ms)
        self.seekbar.setValue(0)
        self._sync_timeline(item if not candidate else None)
        self._set_time_labels(0)
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        self.player.setPosition(0)
        if candidate:
            set_ui_text(self.media_title_label, f"裁剪结果 · {os.path.basename(path)}")
            set_ui_text(self.media_details_label, "临时结果预览；原片尚未替换")
            self._stage_hint("正在打开裁剪结果…")
        elif item:
            set_ui_text(self.media_title_label, f"原片 · {item.get('name', os.path.basename(path))}")
            self._refresh_media_details()
            self._stage_hint("正在打开原片…")
        else:
            set_ui_text(self.media_title_label, os.path.basename(path))
            set_ui_text(self.media_details_label, "本机视频预览")
            self._stage_hint("正在打开视频…")
        if item and not candidate:
            self._set_draft(item.get("draft_start_ms"), item.get("draft_end_ms"), update_item=False)
        elif candidate:
            self._set_draft(None, None, update_item=False)
        self._mark_controls(not self._busy and not candidate and item is not None)
        self._refresh_preview_buttons()
        self._refresh_summary()
        self._refresh_actions()

    def release_media(self):
        self._reset_seeking()
        self.player.stop()
        clear = getattr(self.player, "clearSource", None)
        clear() if callable(clear) else self.player.setSource(QUrl())
        self._loaded_path = None
        self._preview_candidate = False
        self._media_released = self._selected_path is not None
        self._playing_segment = False
        self._duration_ms = self._fallback_duration_ms = 0
        self.seekbar.set_duration(0)
        self.seekbar.set_ranges([])
        self._set_time_labels(0)
        self._stage_hint("选择左侧视频开始预览")
        self.preview_source_button.setVisible(False)
        self.preview_result_button.setVisible(False)
        self._mark_controls(False)

    def _current_item(self):
        return self._items.get(self._selected_path or "")

    def _refresh_ranges(self, item=None):
        self._range_index = None
        if item is not None:
            self._draft_start = item.get("draft_start_ms")
            self._draft_end = item.get("draft_end_ms")
        else:
            self._draft_start = self._draft_end = None
        self._show_draft_fields()
        self._render_range_list()
        self._sync_timeline(item)
        self._refresh_draft_status()
        self._refresh_actions()

    def _clear_player_error(self):
        self.player_error_label.clear()
        self.player_error_label.setVisible(False)

    def _source_path(self, path: str | None) -> str | None:
        return self._path_lookup.get(canonical(path)) if path else None

    def _media_duration(self, item, candidate: bool) -> int:
        if not item:
            return 0
        if candidate:
            return max(0, int((item.get("result") or {}).get("duration_ms") or 0))
        return max(0, int(item.get("duration_ms") or 0))

    def _select_source(self, path: str):
        changed = path != self._selected_path
        self._selected_path = path
        self._range_index = None
        item = self._items.get(path)
        if item:
            self._draft_start = item.get("draft_start_ms")
            self._draft_end = item.get("draft_end_ms")
            self._render_range_list()
        self._clear_error()
        self.load_item(path)
        if changed or path:
            self.selection_changed.emit(path)

    def _file_selected(self, current, previous):
        del previous
        if current is not None:
            path = current.data(Qt.ItemDataRole.UserRole)
            if path:
                self._select_source(str(path))

    def _ranges(self, item=None) -> list[dict[str, int]]:
        item = item or self._current_item()
        if not item:
            return []
        ranges = []
        for segment in item.get("ranges") or []:
            try:
                start, end = int(segment["start_ms"]), int(segment["end_ms"])
            except (KeyError, TypeError, ValueError):
                continue
            if start >= 0 and end > start:
                ranges.append({"start_ms": start, "end_ms": end})
        return sorted(ranges, key=lambda part: part["start_ms"])

    def _render_range_list(self):
        self.range_list.blockSignals(True)
        self.range_list.clear()
        ranges = self._ranges()
        for index, segment in enumerate(ranges):
            length = segment["end_ms"] - segment["start_ms"]
            row = QListWidgetItem(f"{index + 1:02d}   {fmt(segment['start_ms'])}  →  {fmt(segment['end_ms'])}    ·    {fmt(length)}")
            row.setData(Qt.ItemDataRole.UserRole, index)
            self.range_list.addItem(row)
        if self._range_index is not None and self._range_index < self.range_list.count():
            self.range_list.setCurrentRow(self._range_index)
        else:
            self._range_index = None
            self.range_list.setCurrentRow(-1)
        set_ui_text(self.range_count_label, f"{len(ranges)} 段")
        self.range_list.blockSignals(False)

    def _range_selected(self, current, previous):
        del previous
        if current is None:
            self._range_index = None
            self._refresh_actions()
            return
        index = int(current.data(Qt.ItemDataRole.UserRole))
        ranges = self._ranges()
        if index >= len(ranges):
            return
        self._range_index = index
        self._draft_start = ranges[index]["start_ms"]
        self._draft_end = ranges[index]["end_ms"]
        self._show_draft_fields()
        self._sync_timeline(self._current_item())
        self._refresh_draft_status()
        self._refresh_actions()

    def _new_range(self):
        item = self._current_item()
        if item is None or self._busy or self._preview_candidate:
            return
        self._range_index = None
        self.range_list.setCurrentRow(-1)
        self._set_draft(None, None)
        self._clear_error()

    def _mark(self, which: str):
        if self._busy or self._preview_candidate or not (self._current_item() or {}).get("content_sha256"):
            return
        pos = max(0, int(self.player.position()))
        if self._duration_ms:
            pos = min(pos, self._duration_ms)
        if which == "start":
            self._set_draft(pos, self._draft_end)
        else:
            self._set_draft(self._draft_start, pos)

    def _edit_draft(self, which: str):
        if self._busy or self._preview_candidate or not (self._current_item() or {}).get("content_sha256"):
            self._show_draft_fields()
            return
        edit = self.start_input if which == "start" else self.end_input
        try:
            value = parse_timecode(edit.text())
            if value is not None and self._duration_ms and value > self._duration_ms:
                raise ValueError("不能超过视频总时长")
        except ValueError as exc:
            edit.setProperty("invalid", True)
            edit.style().unpolish(edit)
            edit.style().polish(edit)
            set_ui_text(self.draft_status, str(exc))
            self.draft_status.setStyleSheet("color:#ff9898;")
            return
        if which == "start":
            self._set_draft(value, self._draft_end)
        else:
            self._set_draft(self._draft_start, value)

    def _set_draft(self, start: int | None, end: int | None, update_item: bool = True):
        self._draft_start = None if start is None else max(0, int(start))
        self._draft_end = None if end is None else max(0, int(end))
        self._show_draft_fields()
        item = self._current_item()
        if item and update_item:
            item["draft_start_ms"] = self._draft_start
            item["draft_end_ms"] = self._draft_end
            if self._ranges(item) or self._draft_start is not None or self._draft_end is not None:
                item["state"] = "marked"
            else:
                item["state"] = "unmarked"
            self._refresh_row(item["path"])
            self.marks_changed.emit(item["path"], self._marks_payload(item))
        self._sync_timeline(item)
        self._refresh_draft_status()
        self._refresh_actions()

    def _show_draft_fields(self):
        self.start_input.blockSignals(True)
        self.end_input.blockSignals(True)
        set_ui_text(self.start_input, fmt(self._draft_start))
        set_ui_text(self.end_input, fmt(self._draft_end))
        for edit in (self.start_input, self.end_input):
            edit.setProperty("invalid", False)
            edit.style().unpolish(edit)
            edit.style().polish(edit)
        self.start_input.blockSignals(False)
        self.end_input.blockSignals(False)

    def _marks_payload(self, item):
        return {"ranges": self._ranges(item), "draft_start_ms": item.get("draft_start_ms"), "draft_end_ms": item.get("draft_end_ms")}

    def _valid_pair(self, start, end):
        return start is not None and end is not None and int(start) < int(end)

    def _add_range(self):
        if self._busy or self._preview_candidate:
            return
        if not self._valid_pair(self._draft_start, self._draft_end):
            self.show_error("请先设置当前片段的开始和结束，且结束要晚于开始。")
            return
        start, end = int(self._draft_start), int(self._draft_end)
        item = self._current_item()
        if not item:
            return
        ranges = self._ranges(item) + [{"start_ms": start, "end_ms": end}]
        item["ranges"] = sorted(ranges, key=lambda r: r["start_ms"])
        item["result"] = None
        item["state"] = "marked"
        self._range_index = next(i for i, r in enumerate(item["ranges"]) if r["start_ms"] == start and r["end_ms"] == end)
        self._render_range_list()
        self._refresh_row(item["path"])
        self._sync_timeline(item)
        self._refresh_summary()
        self._refresh_actions()
        self.marks_changed.emit(item["path"], self._marks_payload(item))
        self._clear_error()

    def _update_range(self):
        if self._busy or self._preview_candidate or self._range_index is None:
            return
        if not self._valid_pair(self._draft_start, self._draft_end):
            self.show_error("请先设置有效的开始和结束时间。")
            return
        start, end = int(self._draft_start), int(self._draft_end)
        item = self._current_item()
        if not item:
            return
        ranges = self._ranges(item)
        ranges[self._range_index] = {"start_ms": start, "end_ms": end}
        item["ranges"] = sorted(ranges, key=lambda r: r["start_ms"])
        item["result"] = None
        item["state"] = "marked" if item["ranges"] else "unmarked"
        self._range_index = item["ranges"].index({"start_ms": start, "end_ms": end})
        self._render_range_list()
        self._refresh_row(item["path"])
        self._sync_timeline(item)
        self._refresh_summary()
        self._refresh_actions()
        self.marks_changed.emit(item["path"], self._marks_payload(item))
        self._clear_error()

    def _delete_range(self):
        if self._busy or self._range_index is None:
            return
        item = self._current_item()
        if not item:
            return
        ranges = self._ranges(item)
        if self._range_index >= len(ranges):
            return
        deleted = ranges.pop(self._range_index)
        item["ranges"] = ranges
        item["result"] = None
        if (item.get("draft_start_ms"), item.get("draft_end_ms")) == (deleted["start_ms"], deleted["end_ms"]):
            item["draft_start_ms"] = item["draft_end_ms"] = None
        item["state"] = "marked" if ranges or item.get("draft_start_ms") is not None or item.get("draft_end_ms") is not None else "unmarked"
        self._range_index = None
        self._draft_start = item.get("draft_start_ms")
        self._draft_end = item.get("draft_end_ms")
        self._show_draft_fields()
        self._render_range_list()
        self._refresh_row(item["path"])
        self._sync_timeline(item)
        self._refresh_summary()
        self._refresh_actions()
        self.marks_changed.emit(item["path"], self._marks_payload(item))

    def _play_draft(self):
        if not self._valid_pair(self._draft_start, self._draft_end) or self._preview_candidate or self._busy:
            self.show_error("请先设置有效的片段起止时间。")
            return
        self._playing_segment = True
        self.player.setPosition(int(self._draft_start))
        self.player.play()
        self._stage_hint("")

    def _sync_timeline(self, item=None):
        item = item or self._current_item()
        ranges = self._ranges(item) if item and not self._preview_candidate else []
        start = self._draft_start if not self._preview_candidate else None
        end = self._draft_end if not self._preview_candidate else None
        self.seekbar.set_ranges(ranges, start, end)

    def _refresh_draft_status(self):
        if self._preview_candidate:
            set_ui_text(self.draft_status, "当前正在预览裁剪结果；回到原片后可修改标记。")
            self.draft_status.setStyleSheet("color:#76d7c3;")
        elif self._draft_start is None and self._draft_end is None:
            set_ui_text(self.draft_status, "尚未标记当前片段")
            self.draft_status.setStyleSheet("color:#94a4b5;")
        elif self._draft_start is None or self._draft_end is None:
            set_ui_text(self.draft_status, "还需设置另一个时间点")
            self.draft_status.setStyleSheet("color:#e8bc77;")
        elif int(self._draft_start) >= int(self._draft_end):
            set_ui_text(self.draft_status, "结束必须晚于开始")
            self.draft_status.setStyleSheet("color:#ff9898;")
        else:
            set_ui_text(self.draft_status, f"当前片段 {fmt(self._draft_start)} → {fmt(self._draft_end)}")
            self.draft_status.setStyleSheet("color:#8bd9c7;")

    def _position_changed(self, position):
        self._set_time_labels(int(position))
        if not self.seekbar.isSliderDown():
            self.seekbar.blockSignals(True)
            self.seekbar.setValue(min(int(position), self.seekbar.maximum()))
            self.seekbar.blockSignals(False)
        if self._playing_segment and self._draft_end is not None and int(position) >= int(self._draft_end):
            self._playing_segment = False
            self.player.pause()
            self.player.setPosition(int(self._draft_end))

    def _duration_changed(self, duration):
        self._duration_ms = int(duration) if int(duration) > 0 else self._fallback_duration_ms
        self.seekbar.set_duration(self._duration_ms)
        self._set_time_labels(self.player.position())
        self._sync_timeline()
        self._refresh_summary()

    def _seekbar_changed(self, value):
        if self._loaded_path:
            self._playing_segment = False
            if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                self.player.pause()
            if self.seekbar.isSliderDown():
                self._scrub_target = int(value)
                if not self._scrub_timer.isActive():
                    self._flush_scrub()
                    self._scrub_timer.start()
            else:
                self.player.setPosition(int(value))
            self._set_time_labels(int(value))

    def _reset_seeking(self):
        self._scrub_timer.stop()
        self._frame_step_timer.stop()
        self._scrub_target = None
        self._scrub_was_playing = False
        self._frame_times = None
        self._frame_step_pending = False
        self._frame_step_target = None

    def _scrub_started(self):
        self._playing_segment = False
        self._scrub_was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if self._loaded_path:
            self.player.pause()

    def _flush_scrub(self):
        target, self._scrub_target = self._scrub_target, None
        if target is not None and self._loaded_path:
            self.player.setPosition(target)

    def _scrub_finished(self):
        self._scrub_timer.stop()
        self._flush_scrub()
        resume, self._scrub_was_playing = self._scrub_was_playing, False
        if resume and self._loaded_path:
            self.player.play()

    def _video_frame_changed(self, frame):
        if frame.isValid() and frame.startTime() >= 0:
            self._frame_times = (int(frame.startTime()), int(frame.endTime()))
            target = self._frame_step_target
            if target is None or frame.startTime() <= target * 1000 < frame.endTime():
                self._finish_frame_step()

    def _finish_frame_step(self):
        self._frame_step_timer.stop()
        self._frame_step_pending = False
        self._frame_step_target = None
        self._refresh_transport()

    def _step_frame(self, direction):
        if not self._loaded_path or self._frame_step_pending:
            return
        self._playing_segment = False
        self.player.pause()
        frame = self._frame_times
        if frame is not None:
            start, end = frame
            if direction < 0:
                target = (start - 1) // 1000
            else:
                if end <= start:
                    rate = float((self._current_item() or {}).get("frame_rate") or 30)
                    end = start + round(1_000_000 / rate)
                target = math.ceil(end / 1000)
        else:
            rate = float((self._current_item() or {}).get("frame_rate") or 30)
            target = self.player.position() + direction * math.ceil(1000 / rate)
        target = max(0, min(max(0, self._duration_ms - 1), int(target)))
        self._frame_step_pending = True
        self._frame_step_target = target
        self._frame_step_timer.start()
        self._refresh_transport()
        self.player.setPosition(target)
        self._set_time_labels(target)

    def _seek(self, amount):
        if not self._loaded_path:
            return
        duration = self._duration_ms or self.seekbar.maximum()
        target = max(0, min(duration, int(self.player.position()) + amount))
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()
        self.player.setPosition(target)
        self._set_time_labels(target)

    def _toggle_playback(self):
        if not self._loaded_path:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()
            self._stage_hint("")

    def _playback_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        set_ui_text(self.play_button, "暂停" if playing else "播放")
        set_ui_text(self.play_button, "空格暂停" if playing else "空格播放", setter="setToolTip")

    def _speed_changed(self, index):
        rate = self.speed_select.itemData(index)
        if rate is not None:
            self.player.setPlaybackRate(float(rate))

    def _media_status_changed(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self._stage_hint("")
            if self.player.duration() > 0:
                self._duration_changed(self.player.duration())
            if status == QMediaPlayer.MediaStatus.LoadedMedia and self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                # LoadedMedia can be emitted synchronously inside setSource.
                # Wait until that call returns before controlling the decoder.
                QTimer.singleShot(0, self._show_loaded_first_frame)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia and not self.player_error_label.text():
            self._media_error(self.player.error(), self.player.errorString() or "此视频无法播放。")

    def _show_loaded_first_frame(self):
        if (self._loaded_path
                and self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
                and self.player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia,
                                                 QMediaPlayer.MediaStatus.BufferedMedia)):
            self.player.pause()

    def _media_error(self, error, text):
        del error
        set_ui_text(self.player_error_label, text or "无法播放此视频；请检查文件或系统编码支持。")
        self.player_error_label.setVisible(True)
        self._stage_hint("无法播放此视频")

    def _stage_hint(self, text):
        set_ui_text(self.stage_hint, text)
        self.stage_hint.setVisible(bool(text))

    def _set_time_labels(self, position):
        set_ui_text(self.position_label, fmt(int(position)))
        set_ui_text(self.duration_label, f"/ {fmt(self._duration_ms)}")

    def _refresh_media_details(self):
        item = self._current_item()
        if not item:
            return
        w, h = int(item.get("width") or 0), int(item.get("height") or 0)
        if abs(int(item.get("rotation") or 0)) % 180 == 90:
            w, h = h, w
        parts = []
        if w and h:
            parts.append(f"{'竖屏' if h > w else '横屏'}  {w}×{h}")
        if item.get("frame_rate"):
            parts.append(f"{float(item['frame_rate']):.2f} fps")
        if item.get("codec"):
            parts.append(str(item["codec"]))
        if item.get("duration_ms"):
            parts.append(fmt(item["duration_ms"]))
        set_ui_text(self.media_details_label, "  ·  ".join(parts) or "本机原片预览")

    def _refresh_row(self, path):
        item = self._items.get(path)
        row = self._rows.get(canonical(path))
        if item is None or row is None:
            return
        row.setData(Qt.ItemDataRole.UserRole + 1, item.get("name", os.path.basename(path)))
        row.setData(Qt.ItemDataRole.UserRole + 2, item_details(item))
        state = item.get("state", "unmarked")
        if (item.get("ranges") or item.get("draft_start_ms") is not None or item.get("draft_end_ms") is not None) and state == "unmarked":
            state = "marked"
        row.setData(Qt.ItemDataRole.UserRole + 3, STATES.get(state, "未标记"))
        row.setData(Qt.ItemDataRole.UserRole + 4, bool(item.get("error")))
        self.file_list.viewport().update()

    def _refresh_summary(self):
        item = self._current_item()
        if not item:
            set_ui_text(self.range_summary, "准备后会显示实际拼接范围和节省空间")
            self._refresh_draft_status()
            return
        ranges = self._ranges(item)
        total_duration = sum(r["end_ms"] - r["start_ms"] for r in ranges)
        result = item.get("result") or {}
        if item.get("fingerprint_pending"):
            set_ui_text(self.range_summary, "正在读取素材，可先观看视频，完成后即可标记。")
        elif item.get("state") == "prepared" and result.get("verified"):
            actual_ranges = result.get("actual_ranges") or []
            if actual_ranges:
                actual = "\n".join(f"{fmt(r.get('actual_start_ms', r.get('start_ms')))} → {fmt(r.get('actual_end_ms', r.get('end_ms')))}" for r in actual_ranges)
            elif result.get("actual_start_ms") is not None and result.get("actual_end_ms") is not None:
                actual = f"{fmt(result['actual_start_ms'])} → {fmt(result['actual_end_ms'])}"
            else:
                actual = f"{len(ranges)} 段，实际范围待核对"
            set_ui_text(self.range_summary, f"实际拼接范围\n{actual}\n可省 {human_size(result.get('saved_bytes'))} · 结果 {human_size(result.get('candidate_size_bytes'))}")
        elif ranges:
            saved = 0
            duration = int(item.get("duration_ms") or self._duration_ms or 0)
            size = int(item.get("size_bytes") or 0)
            if duration:
                saved = int(size * max(0.0, 1.0 - total_duration / duration))
            set_ui_text(self.range_summary, f"已添加 {len(ranges)} 段 · 共保留 {fmt(total_duration)}\n预计可省 {human_size(saved)}")
        else:
            set_ui_text(self.range_summary, "添加保留片段后，会显示预计节省空间")
        self._refresh_draft_status()

    def _mark_controls(self, enabled):
        item = self._current_item()
        enabled = bool(enabled and item and item.get("content_sha256"))
        for control in (self.start_input, self.end_input, self.mark_in_button, self.mark_out_button, self.new_range_button):
            control.setEnabled(enabled)
        self.add_range_button.setEnabled(enabled and self._valid_pair(self._draft_start, self._draft_end))
        self.play_segment_button.setEnabled(enabled and self._valid_pair(self._draft_start, self._draft_end))
        self.update_range_button.setEnabled(enabled and self._range_index is not None and self._valid_pair(self._draft_start, self._draft_end))
        self.delete_range_button.setEnabled(enabled and self._range_index is not None)

    def _valid_marked_paths(self):
        result = []
        for path, item in self._items.items():
            if item.get("content_sha256") and self._ranges(item) and item.get("state") not in ("preparing", "prepared", "committing", "complete", "completed"):
                result.append(path)
        return result

    def _prepared_items(self):
        return [item for item in self._items.values() if item.get("state") == "prepared" and (item.get("result") or {}).get("verified") and (item.get("result") or {}).get("candidate_path")]

    def _refresh_preview_buttons(self):
        item = self._current_item()
        result = item.get("result") or {} if item else {}
        has_result = bool(result.get("verified") and result.get("candidate_path"))
        self.preview_result_button.setVisible(has_result and not self._preview_candidate and not self._media_released)
        show_source = bool(self._selected_path and (self._preview_candidate or self._media_released))
        set_ui_text(self.preview_source_button, "看原片" if self._preview_candidate else "重新打开原片")
        self.preview_source_button.setVisible(show_source)
        self.preview_result_button.setEnabled(not self._busy)
        self.preview_source_button.setEnabled(not self._busy)

    def _refresh_actions(self):
        self.language_combo.setEnabled(not self._busy)
        self.remove_files_button.setEnabled(bool(self.file_list.selectedItems()) and not self._busy)
        self.restore_files_button.setEnabled(bool(self._folder and self._removed_count) and not self._busy)
        self._refresh_transport()
        self._mark_controls(not self._busy and not self._preview_candidate)
        marked, prepared = self._valid_marked_paths(), self._prepared_items()
        self.prepare_button.setEnabled(bool(marked) and not self._busy)
        set_ui_text(self.prepare_button, f"准备已标记的视频（{len(marked)}）" if marked else "准备已标记的视频")
        self.export_button.setVisible(bool(prepared))
        self.replace_button.setVisible(bool(prepared))
        self.export_button.setEnabled(bool(prepared) and not self._busy)
        self.replace_button.setEnabled(bool(prepared) and not self._busy)
        if prepared:
            set_ui_text(self.export_button, f"保留为新文件（{len(prepared)}）")
            set_ui_text(self.replace_button, f"确认替换原片（{len(prepared)}）")
        self._refresh_preview_buttons()

    def _refresh_transport(self):
        ready = bool(self._loaded_path)
        self.play_button.setEnabled(ready)
        self.back_button.setEnabled(ready)
        self.forward_button.setEnabled(ready)
        self.seekbar.setEnabled(ready)
        self.previous_frame_button.setEnabled(ready and not self._frame_step_pending)
        self.next_frame_button.setEnabled(ready and not self._frame_step_pending)

    def _prepare(self):
        if self._busy:
            return
        paths = self._valid_marked_paths()
        if not paths:
            self.show_error("请先添加至少一个有效的保留片段。")
            return
        self._clear_error()
        self.prepare_requested.emit(paths)

    def _preview_result(self):
        item = self._current_item()
        result = item.get("result") or {} if item else {}
        path = result.get("candidate_path")
        if path and result.get("verified") and not self._busy:
            self.load_item(path)

    def _preview_source(self):
        if self._selected_path and not self._busy:
            self.load_item(self._selected_path)

    def _commit(self, mode):
        if self._busy:
            return
        paths = [item["path"] for item in self._prepared_items()]
        if paths:
            # Replacement confirmation and its detailed file/range list belong to the controller.
            self.commit_requested.emit(mode, paths)

    def _clear_error(self):
        set_ui_text(self.feedback_label, "")
        self.feedback_label.setVisible(False)

    def closeEvent(self, event):  # noqa: N802 - Qt override
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.release_media()
        super().closeEvent(event)
