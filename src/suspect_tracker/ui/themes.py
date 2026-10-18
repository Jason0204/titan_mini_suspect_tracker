from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    key: str
    display_name: str
    window: str
    panel: str
    panel_alt: str
    video: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    accent: str
    accent_soft: str
    button: str
    button_hover: str
    selection: str


THEMES: dict[str, Theme] = {
    "graphite": Theme(
        key="graphite",
        display_name="Graphite / 石墨青",
        window="#10161d",
        panel="#151d25",
        panel_alt="#0f151b",
        video="#18212a",
        border="#2d3c48",
        border_strong="#4f9f82",
        text="#e8eef2",
        text_muted="#8fa0ad",
        accent="#39b57f",
        accent_soft="#9cc8b3",
        button="#202b35",
        button_hover="#294052",
        selection="#315e4e",
    ),
    "daylight": Theme(
        key="daylight",
        display_name="Daylight / 日间蓝",
        window="#eef3f6",
        panel="#ffffff",
        panel_alt="#f7fafb",
        video="#dfe8ee",
        border="#c8d5dc",
        border_strong="#2f7f9f",
        text="#1b2a34",
        text_muted="#5f7180",
        accent="#167d9d",
        accent_soft="#2c6d82",
        button="#edf4f7",
        button_hover="#dbeaf0",
        selection="#c7e5ef",
    ),
    "amber": Theme(
        key="amber",
        display_name="VS Code Black / VS Code 黑色",
        window="#1e1e1e",
        panel="#252526",
        panel_alt="#1b1b1b",
        video="#181818",
        border="#3c3c3c",
        border_strong="#007acc",
        text="#d4d4d4",
        text_muted="#969696",
        accent="#007acc",
        accent_soft="#4fc1ff",
        button="#2d2d30",
        button_hover="#37373d",
        selection="#264f78",
    ),
}


def build_stylesheet(theme: Theme) -> str:
    return f"""
QMainWindow, QWidget {{
    background: {theme.window};
    color: {theme.text};
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 13px;
}}
QLabel#Title {{
    font-size: 20px;
    font-weight: 700;
    color: {theme.text};
}}
QLabel#VideoSurface {{
    background: {theme.video};
    border: 1px solid {theme.border};
    border-radius: 6px;
    color: {theme.text_muted};
}}
/* Changed 2026-09-20: Keep paused-frame selection guidance visually secondary. */
QLabel#SelectionHint {{
    color: {theme.text_muted};
    padding: 3px 2px;
}}
QGroupBox {{
    border: 1px solid {theme.border};
    border-radius: 6px;
    margin-top: 18px;
    padding: 10px;
    background: {theme.panel};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {theme.accent_soft};
}}
QPushButton, QComboBox, QLineEdit {{
    background: {theme.button};
    border: 1px solid {theme.border};
    border-radius: 5px;
    color: {theme.text};
    min-height: 28px;
    padding: 4px 8px;
}}
QPushButton:hover, QComboBox:hover, QLineEdit:hover {{
    background: {theme.button_hover};
    border-color: {theme.border_strong};
}}
QPushButton:pressed {{
    background: {theme.selection};
}}
QTableWidget, QListWidget, QPlainTextEdit {{
    background: {theme.panel_alt};
    border: 1px solid {theme.border};
    border-radius: 4px;
    color: {theme.text};
    selection-background-color: {theme.selection};
}}
QHeaderView::section {{
    background: {theme.button};
    color: {theme.text};
    border: 0;
    padding: 5px;
}}
QSplitter::handle {{
    background: {theme.border};
}}
"""
