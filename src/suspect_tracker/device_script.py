"""把设备页面脚本拼装成可上传的单文件。

设备侧（MicroPython）没有包管理，页面脚本一直是"一个文件跑到底"：上位机读文件、
替换几个占位符、整份上传。加入触摸驱动后出现了第一个需要在两个页面之间共用的模块，
直接复制会让驱动分叉，所以用标记内联：

    # @@INLINE:ft5406.py@@

`inline_modules()` 把该行换成 `src/device/ft5406.py` 的全文。仓库里只有一份驱动，
板子收到的仍然是一份自包含脚本（SD 卡上不需要额外文件，也就不会版本漂移）。

硬约束：被内联的文件必须是纯定义（常量、函数、类），不能有模块级副作用，
因为它的内容会被插到宿主脚本的模块作用域里。
"""

from __future__ import annotations

import re
from pathlib import Path

DEVICE_DIR = Path(__file__).resolve().parents[1] / "device"
MARKER = re.compile(r"# @@INLINE:([\w.]+)@@")


def inline_modules(source: str, base_dir: Path | None = None) -> str:
    """把 source 里的所有 `# @@INLINE:<file>@@` 换成对应文件的内容。"""
    base = base_dir if base_dir is not None else DEVICE_DIR
    for name in MARKER.findall(source):
        path = base / name
        if not path.is_file():
            raise FileNotFoundError(f"device module to inline not found: {path}")
        source = source.replace(f"# @@INLINE:{name}@@", path.read_text(encoding="utf-8"))
    return source
