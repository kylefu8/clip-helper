"""Chinese/English presentation without changing saved media data."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QLocale, QSettings
from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QPushButton, QWidget

from translations import EN

_language = "zh"
_settings = None
_patterns = None


def get_language() -> str:
    return _language


def set_language(language: str) -> None:
    global _language
    if language not in ("zh", "en"):
        raise ValueError("Language must be zh or en")
    _language = language
    if _settings is not None:
        _settings.setValue("language", language)
        _settings.sync()


def configure(directory: Path, override: str | None = None) -> None:
    global _settings, _language
    _settings = QSettings(str(directory / "preferences.ini"), QSettings.Format.IniFormat)
    system = "zh" if QLocale.system().language() == QLocale.Language.Chinese else "en"
    saved = str(_settings.value("language", system))
    _language = override or (saved if saved in ("zh", "en") else system)


def _message_patterns():
    global _patterns
    if _patterns is None:
        patterns = []
        for source, target in EN.items():
            variants = [(source, target)]
            if source != source.strip("\n"):
                variants.append((source.strip("\n"), target.strip("\n")))
            for original, translated in variants:
                tokens = re.split(r"(\{\d+\})", original)
                fields = [int(token[1:-1]) for token in tokens if re.fullmatch(r"\{\d+\}", token)]
                if not fields:
                    continue
                pattern = "".join("(.*?)" if re.fullmatch(r"\{\d+\}", token) else re.escape(token)
                                  for token in tokens)
                specificity = sum(len(token) for token in tokens if not re.fullmatch(r"\{\d+\}", token))
                patterns.append((specificity, re.compile(pattern, re.DOTALL), fields, translated))
        _patterns = sorted(patterns, key=lambda entry: entry[0], reverse=True)
    return _patterns


def tr(message: str, _depth: int = 0) -> str:
    """Translate canonical labels and already-formatted progress/errors at display time."""
    message = str(message)
    if _language == "zh" or _depth > 5 or not re.search(r"[\u4e00-\u9fff]", message):
        return message
    if message in EN:
        return EN[message]
    if message.strip("\n") in EN:
        return EN[message.strip("\n")]
    if message + "\n" in EN:
        return EN[message + "\n"].rstrip("\n")
    # Filenames/paths remain user data, including Chinese folder names.
    if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/)", message):
        return message
    for _, pattern, fields, target in _message_patterns():
        match = pattern.fullmatch(message)
        if match:
            values = [""] * (max(fields) + 1)
            for field, captured in zip(fields, match.groups()):
                values[field] = tr(captured, _depth + 1)
            return target.format(*values)
    # Composite status lines wrap backend messages with counters and filenames.
    for separator in (" 保存位置：", "\n", " · ", "："):
        if separator in message:
            translated_separator = ": " if separator == "：" else separator
            if separator == " 保存位置：":
                translated_separator = " Saved to: "
            return translated_separator.join(tr(part, _depth + 1) for part in message.split(separator))
    return message


def set_ui_text(widget, text: str, *, setter: str = "setText") -> None:
    text = str(text)
    if isinstance(widget, QWidget) and not (isinstance(widget, QLineEdit) and setter == "setText"):
        entries = dict(widget.property("_i18n_texts") or {})
        entries[setter] = text
        widget.setProperty("_i18n_texts", entries)
    getattr(widget, setter)(tr(text))


def localized_label(text: str = "", parent=None) -> QLabel:
    widget = QLabel(parent)
    set_ui_text(widget, text)
    return widget


def localized_button(text: str = "", parent=None) -> QPushButton:
    widget = QPushButton(parent)
    set_ui_text(widget, text)
    return widget


def retranslate(root: QWidget) -> None:
    for widget in [root, *root.findChildren(QWidget)]:
        for setter, text in (widget.property("_i18n_texts") or {}).items():
            getattr(widget, setter)(tr(text))


def message_box(icon, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                default=QMessageBox.StandardButton.NoButton):
    dialog = QMessageBox(icon, tr(title), tr(text), buttons, parent)
    for standard, label in ((QMessageBox.StandardButton.Ok, "确定"),
                            (QMessageBox.StandardButton.Cancel, "取消"),
                            (QMessageBox.StandardButton.Yes, "替换原片")):
        button = dialog.button(standard)
        if button is not None:
            button.setText(tr(label))
    if default != QMessageBox.StandardButton.NoButton:
        dialog.setDefaultButton(default)
    return dialog.exec()
