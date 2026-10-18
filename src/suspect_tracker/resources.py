"""Locate bundled assets for both source runs and frozen (PyInstaller) builds.

Assets live in ``src/suspect_tracker/assets/``.  When packaged with
``build_exe.ps1`` the folder is collected as ``assets`` next to the executable
(``sys._MEIPASS`` in onefile builds), and this module is the single place that
knows about both layouts.
"""

from __future__ import annotations

import sys
from pathlib import Path

ASSET_DIR_NAME = "assets"


def asset_path(name: str) -> Path:
    """Return the absolute path of ``name`` inside the assets directory."""
    relative = Path(ASSET_DIR_NAME) / name
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / relative
        if bundle.is_file():
            return bundle
    return Path(__file__).resolve().parent / relative
