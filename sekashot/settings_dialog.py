"""Settings dialog - everything Lightshot's option window exposes, and a bit more."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_DISPLAY_NAME, APP_TAGLINE
from .annotations import PALETTE
from .hotkey import wayland_hotkey_hint


class ColorButton(QPushButton):
    def __init__(self, colour: str) -> None:
        super().__init__()
        self._colour = QColor(colour)
        self.setFixedSize(64, 26)
        self.clicked.connect(self._pick)
        self._refresh()

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(self._colour, self, "Default annotation colour")
        if chosen.isValid():
            self._colour = chosen
            self._refresh()

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"background:{self._colour.name()}; border:1px solid #888; border-radius:4px;"
        )
        self.setText("")

    def colour(self) -> str:
        return self._colour.name()


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"{APP_DISPLAY_NAME} - Settings")
        self.setMinimumWidth(520)

        tabs = QTabWidget(self)

        # ---------------- capture / drawing --------------------------
        drawing = QWidget()
        form = QFormLayout(drawing)

        self.pen_width = QSpinBox()
        self.pen_width.setRange(1, 24)
        self.pen_width.setValue(cfg.pen_width)
        form.addRow("Pen / line width (px)", self.pen_width)

        self.marker_width = QSpinBox()
        self.marker_width.setRange(4, 80)
        self.marker_width.setValue(cfg.marker_width)
        form.addRow("Marker width (px)", self.marker_width)

        self.marker_alpha = QSpinBox()
        self.marker_alpha.setRange(10, 255)
        self.marker_alpha.setValue(cfg.marker_alpha)
        form.addRow("Marker opacity", self.marker_alpha)

        self.blur_radius = QSpinBox()
        self.blur_radius.setRange(2, 60)
        self.blur_radius.setValue(cfg.blur_radius)
        self.blur_radius.setToolTip("Blur strength in pixels (Ctrl+wheel over the canvas)")
        form.addRow("Blur strength (px)", self.blur_radius)

        self.font_size = QSpinBox()
        self.font_size.setRange(8, 96)
        self.font_size.setValue(cfg.font_size)
        form.addRow("Text size (px)", self.font_size)

        self.font_family = QComboBox()
        self.font_family.setEditable(True)
        for family in ("Sans", "Serif", "Monospace", "DejaVu Sans", "Noto Sans", "Ubuntu"):
            self.font_family.addItem(family)
        self.font_family.setCurrentText(cfg.font_family)
        form.addRow("Text font", self.font_family)

        colour_row = QHBoxLayout()
        self.colour = ColorButton(cfg.color)
        colour_row.addWidget(self.colour)
        for value in PALETTE:
            chip = QPushButton()
            chip.setFixedSize(22, 22)
            chip.setStyleSheet(f"background:{value}; border:1px solid #888; border-radius:3px;")
            chip.setToolTip(value)
            chip.clicked.connect(lambda _c=False, v=value: self._set_colour(v))
            colour_row.addWidget(chip)
        colour_row.addStretch(1)
        holder = QWidget()
        holder.setLayout(colour_row)
        form.addRow("Default colour", holder)

        self.dim_alpha = QSpinBox()
        self.dim_alpha.setRange(0, 230)
        self.dim_alpha.setValue(cfg.dim_alpha)
        form.addRow("Dim background", self.dim_alpha)

        self.badge = QCheckBox("Show the W x H size badge")
        self.badge.setChecked(cfg.show_size_badge)
        form.addRow("", self.badge)

        self.ants = QCheckBox("Animate the selection border (marching ants)")
        self.ants.setChecked(cfg.marching_ants)
        form.addRow("", self.ants)

        self.focus_monitor = QCheckBox("Capture only the monitor the cursor is on (multi-monitor)")
        self.focus_monitor.setChecked(cfg.capture_focused_output)
        form.addRow("", self.focus_monitor)

        self.sound = QCheckBox("Play the shutter sound")
        self.sound.setChecked(cfg.play_sound)
        form.addRow("", self.sound)

        tabs.addTab(drawing, "Editor")

        # ---------------- saving ------------------------------------
        saving = QWidget()
        form2 = QFormLayout(saving)

        row = QHBoxLayout()
        self.save_dir = QLineEdit(cfg.save_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dir)
        row.addWidget(self.save_dir)
        row.addWidget(browse)
        holder2 = QWidget()
        holder2.setLayout(row)
        form2.addRow("Save folder", holder2)

        self.save_format = QComboBox()
        self.save_format.addItems(["png", "jpg"])
        self.save_format.setCurrentText(cfg.save_format)
        form2.addRow("Format", self.save_format)

        self.jpg_quality = QSpinBox()
        self.jpg_quality.setRange(40, 100)
        self.jpg_quality.setValue(cfg.jpg_quality)
        form2.addRow("JPEG quality", self.jpg_quality)

        self.auto_save = QCheckBox("Automatically save every capture")
        self.auto_save.setChecked(cfg.auto_save)
        form2.addRow("", self.auto_save)

        row3 = QHBoxLayout()
        self.auto_save_dir = QLineEdit(cfg.auto_save_dir)
        browse3 = QPushButton("Browse…")
        browse3.clicked.connect(lambda: self._browse_into(self.auto_save_dir))
        row3.addWidget(self.auto_save_dir)
        row3.addWidget(browse3)
        holder3 = QWidget()
        holder3.setLayout(row3)
        form2.addRow("Auto-save folder", holder3)

        self.copy_format = QComboBox()
        self.copy_format.addItem("Copy the image itself", "image")
        self.copy_format.addItem("Copy the saved file path", "file-url")
        index = self.copy_format.findData(cfg.copy_format)
        self.copy_format.setCurrentIndex(max(0, index))
        form2.addRow("Copy button copies", self.copy_format)

        tabs.addTab(saving, "Saving")

        # ---------------- hotkey help --------------------------------
        hotkey_page = QWidget()
        vbox = QVBoxLayout(hotkey_page)
        info = QLabel(wayland_hotkey_hint() if True else "")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        vbox.addWidget(info)
        vbox.addWidget(
            QLabel(
                "\nOn X11 the application grabs the key itself.\n"
                "You can always trigger a capture with:  sekashot --capture"
            )
        )
        vbox.addStretch(1)
        tabs.addTab(hotkey_page, "Hotkey")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        title = QLabel(f"<b>{APP_DISPLAY_NAME}</b> — {APP_TAGLINE}")
        layout.addWidget(title)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _set_colour(self, value: str) -> None:
        self.colour._colour = QColor(value)  # noqa: SLF001
        self.colour._refresh()              # noqa: SLF001

    def _browse_dir(self) -> None:
        self._browse_into(self.save_dir)

    def _browse_into(self, field: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", field.text() or str(""))
        if chosen:
            field.setText(chosen)

    def _save(self) -> None:
        cfg = self.cfg
        cfg.pen_width = self.pen_width.value()
        cfg.marker_width = self.marker_width.value()
        cfg.marker_alpha = self.marker_alpha.value()
        cfg.blur_radius = self.blur_radius.value()
        cfg.font_size = self.font_size.value()
        cfg.font_family = self.font_family.currentText()
        cfg.color = self.colour.colour()
        cfg.dim_alpha = self.dim_alpha.value()
        cfg.show_size_badge = self.badge.isChecked()
        cfg.marching_ants = self.ants.isChecked()
        cfg.capture_focused_output = self.focus_monitor.isChecked()
        cfg.play_sound = self.sound.isChecked()

        cfg.save_dir = self.save_dir.text() or cfg.save_dir
        cfg.save_format = self.save_format.currentText()
        cfg.jpg_quality = self.jpg_quality.value()
        cfg.auto_save = self.auto_save.isChecked()
        cfg.auto_save_dir = self.auto_save_dir.text() or cfg.auto_save_dir

        cfg.copy_format = self.copy_format.currentData() or "image"

        cfg.save()
        self.accept()
