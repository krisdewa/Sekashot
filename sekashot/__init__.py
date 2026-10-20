"""Sekashot - a fast, customisable screenshot tool for Linux.

Inspired by Lightshot. Written in Python 3 + PyQt6:
  * runs in the system tray
  * global "Print Screen" hotkey (compositor keybind on Wayland)
  * frozen, dimmed full-screen overlay
  * click-drag selection with a live "W x H" size badge
  * vertical annotation toolbar (pen/line/arrow/rect/marker/text/blur/colour/undo)
  * horizontal action toolbar (copy/save/close)
"""

__all__ = ["__version__", "APP_NAME", "APP_ID", "APP_DISPLAY_NAME"]

__version__ = "1.0.0"
APP_NAME = "sekashot"
APP_ID = "sekashot"
APP_DISPLAY_NAME = "Sekashot"
APP_TAGLINE = "The fastest way to take a customizable screenshot"
