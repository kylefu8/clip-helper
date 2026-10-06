"""Small presentation widgets used by the manual video trimmer."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSlider, QStyle, QStyledItemDelegate


class RangeSlider(QSlider):
    """Millisecond seek slider with marked keep ranges and a draft range."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._ranges: list[tuple[int, int]] = []
        self._draft_start: int | None = None
        self._draft_end: int | None = None
        self.setRange(0, 0)
        self.setSingleStep(1)
        self.setPageStep(5_000)
        self.setTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(34)
        self.setMouseTracking(True)

    def set_ranges(self, ranges, draft_start_ms: int | None = None, draft_end_ms: int | None = None) -> None:
        limit = self.maximum()
        normalized = []
        for segment in ranges or []:
            if isinstance(segment, dict):
                start, end = segment.get("start_ms"), segment.get("end_ms")
            else:
                start, end = segment
            if start is None or end is None:
                continue
            start = max(0, min(limit, int(start)))
            end = max(0, min(limit, int(end)))
            if start < end:
                normalized.append((start, end))
        self._ranges = sorted(normalized)
        self._draft_start = self._clamp(draft_start_ms)
        self._draft_end = self._clamp(draft_end_ms)
        self.update()

    def set_range_markers(self, start_ms: int | None, end_ms: int | None) -> None:
        self.set_ranges([], start_ms, end_ms)

    def set_duration(self, duration_ms: int) -> None:
        duration_ms = max(0, int(duration_ms or 0))
        value = min(self.value(), duration_ms)
        self.setRange(0, duration_ms)
        self.setValue(value)
        self.set_ranges(self._ranges, self._draft_start, self._draft_end)

    def _clamp(self, value: int | None) -> int | None:
        if value is None:
            return None
        return max(0, min(self.maximum(), int(value)))

    def _track(self) -> tuple[float, float, float]:
        left = 13.0
        right = max(left + 1.0, float(self.width()) - 13.0)
        return left, right, float(self.height()) / 2.0

    def _x_for(self, value: int) -> float:
        left, right, _ = self._track()
        span = self.maximum() - self.minimum()
        if span <= 0:
            return left
        ratio = max(0.0, min(1.0, (int(value) - self.minimum()) / span))
        return left + ratio * (right - left)

    def _value_for_x(self, x: float) -> int:
        left, right, _ = self._track()
        if right <= left or self.maximum() <= self.minimum():
            return self.minimum()
        ratio = max(0.0, min(1.0, (x - left) / (right - left)))
        return round(self.minimum() + ratio * (self.maximum() - self.minimum()))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        left, right, cy = self._track()
        height = 6.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#27313f"))
        painter.drawRoundedRect(QRectF(left, cy - height / 2, right - left, height), height / 2, height / 2)
        for start, end in self._ranges:
            x1, x2 = self._x_for(start), self._x_for(end)
            painter.setBrush(QColor("#1a756b"))
            painter.drawRoundedRect(QRectF(x1, cy - height / 2, max(1, x2 - x1), height), height / 2, height / 2)
        draft_start = self._clamp(self._draft_start)
        draft_end = self._clamp(self._draft_end)
        if draft_start is not None and draft_end is not None and draft_start < draft_end:
            x1, x2 = self._x_for(draft_start), self._x_for(draft_end)
            painter.setBrush(QColor("#527044"))
            painter.drawRoundedRect(QRectF(x1, cy - height / 2, max(1, x2 - x1), height), height / 2, height / 2)
        x = self._x_for(self.value())
        if x > left:
            painter.setBrush(QColor("#91a7bd"))
            painter.drawRoundedRect(QRectF(left, cy - 1, x - left, 2), 1, 1)
        markers = []
        for start, end in self._ranges:
            markers.extend(((start, QColor("#42d6bd"), True), (end, QColor("#ffb454"), False)))
        markers.extend(((draft_start, QColor("#a6db72"), True), (draft_end, QColor("#ffd27b"), False)))
        for marker, color, upper in markers:
            if marker is None:
                continue
            x = self._x_for(marker)
            top = cy - (13 if upper else 9)
            bottom = cy + (9 if upper else 13)
            painter.setPen(QPen(color, 2))
            painter.drawLine(round(x), round(top), round(x), round(bottom))
        x = self._x_for(self.value())
        painter.setPen(QPen(QColor("#e8f1fa"), 1.5))
        painter.setBrush(QColor("#e8f1fa"))
        painter.drawEllipse(QRectF(x - 5, cy - 5, 10, 10))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#6fb7ff"), 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(1, 2, self.width() - 2, self.height() - 4), 5, 5)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.setSliderDown(True)
            value = self._value_for_x(event.position().x())
            if value != self.value():
                self.setValue(value)
                self.sliderMoved.emit(value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.isSliderDown() and event.buttons() & Qt.MouseButton.LeftButton:
            value = self._value_for_x(event.position().x())
            if value != self.value():
                self.setValue(value)
                self.sliderMoved.emit(value)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            value = self._value_for_x(event.position().x())
            if value != self.value():
                self.setValue(value)
                self.sliderMoved.emit(value)
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class VideoItemDelegate(QStyledItemDelegate):
    """Compact two-line row for scanning a folder's videos."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt override
        return QSize(option.rect.width(), 70)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802 - Qt override
        painter.save()
        rect = option.rect.adjusted(4, 3, -4, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#212d3b" if selected else "#171f2a" if hovered else "#151c25"))
        painter.drawRoundedRect(rect, 9, 9)
        if selected:
            painter.setBrush(QColor("#50cfb9"))
            painter.drawRoundedRect(QRectF(rect.left() + 1, rect.top() + 12, 3, rect.height() - 24), 1.5, 1.5)

        name = str(index.data(Qt.ItemDataRole.UserRole + 1) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        details = str(index.data(Qt.ItemDataRole.UserRole + 2) or "")
        status = str(index.data(Qt.ItemDataRole.UserRole + 3) or "未标记")
        error = bool(index.data(Qt.ItemDataRole.UserRole + 4))
        right = rect.right() - 14
        font = option.font
        font.setPointSize(max(9, font.pointSize()))
        if selected:
            font.setWeight(font.Weight.DemiBold)
        painter.setFont(font)
        badge_font = font
        badge_font.setPointSize(max(8, font.pointSize() - 1))
        badge_metrics = QFontMetrics(badge_font)
        badge_width = badge_metrics.horizontalAdvance(status) + 18
        badge = QRectF(right - badge_width, rect.top() + 10, badge_width, 22)
        if error or status == "有问题":
            badge_color, badge_text = QColor("#592f35"), QColor("#ffb4b4")
        elif status in ("已标记", "候选就绪", "已完成"):
            badge_color, badge_text = QColor("#1e514b"), QColor("#7fe0c9")
        elif status == "处理中…":
            badge_color, badge_text = QColor("#51412b"), QColor("#ffd08d")
        else:
            badge_color, badge_text = QColor("#2b3542"), QColor("#a9b6c5")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(badge_color)
        painter.drawRoundedRect(badge, 8, 8)
        painter.setFont(badge_font)
        painter.setPen(badge_text)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, status)

        left = rect.left() + (18 if selected else 14)
        name_width = max(20, int(badge.left() - left - 10))
        painter.setFont(font)
        painter.setPen(QColor("#eef4fb"))
        painter.drawText(left, rect.top() + 30, QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideMiddle, name_width))
        detail_font = font
        detail_font.setPointSize(max(8, detail_font.pointSize() - 1))
        painter.setFont(detail_font)
        painter.setPen(QColor("#8393a6"))
        painter.drawText(left, rect.top() + 52, QFontMetrics(detail_font).elidedText(details, Qt.TextElideMode.ElideRight, max(20, rect.width() - 30)))
        painter.restore()
