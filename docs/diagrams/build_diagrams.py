"""生成 docs/diagrams/src/*.svg —— 工程介绍文档用的框图。

设计约束（Qt QSvgRenderer 的渲染能力有限）：
  * 只用基础图形 / line / polyline / text，不用 filter、foreignObject、CSS <style>；
  * 所有字体、颜色写成内联属性；
  * 画布宽度统一 1200，输出 2 倍 PNG（见 render_svg.py）。

    ui_venv/Scripts/python.exe docs/diagrams/build_diagrams.py
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------- 配色 / 字体

INK = "#1F2937"
MUTED = "#64748B"
FAINT = "#94A3B8"
LINE = "#CBD5E1"
WHITE = "#FFFFFF"

BLUE = "#2563EB"
BLUE_BG = "#EFF6FF"
PURPLE = "#7C3AED"
PURPLE_BG = "#F5F3FF"
GREEN = "#0F766E"
GREEN_BG = "#ECFDF5"
ORANGE = "#EA580C"
ORANGE_BG = "#FFF7ED"
CYAN = "#0E7490"
CYAN_BG = "#ECFEFF"
RED = "#DC2626"
RED_BG = "#FEF2F2"
SLATE = "#475569"
SLATE_BG = "#F1F5F9"

UI = "Microsoft YaHei"
MONO = "Consolas"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_width(value: str, size: float) -> float:
    """粗略估算文本宽度：CJK 记 1.0 em，其余记 0.55 em。"""
    width = 0.0
    for ch in value:
        width += 1.0 if ord(ch) > 0x2E80 else 0.55
    return width * size


class Svg:
    def __init__(self, width: int, height: int, bg: str = WHITE) -> None:
        self.width = width
        self.height = height
        self.bg = bg
        self._body: list[str] = []

    # -- 基础图形 ---------------------------------------------------------

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str = WHITE,
        stroke: str | None = None,
        rx: float = 8,
        sw: float = 1.2,
        dash: str | None = None,
        opacity: float = 1.0,
    ) -> None:
        attrs = [
            f'x="{x:.1f}"',
            f'y="{y:.1f}"',
            f'width="{w:.1f}"',
            f'height="{h:.1f}"',
            f'rx="{rx}"',
            f'fill="{fill}"',
            f'fill-opacity="{opacity}"',
        ]
        if stroke:
            attrs.append(f'stroke="{stroke}"')
            attrs.append(f'stroke-width="{sw}"')
        if dash:
            attrs.append(f'stroke-dasharray="{dash}"')
        self._body.append(f'<rect {" ".join(attrs)}/>')

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str = LINE,
        width: float = 1.2,
        dash: str | None = None,
    ) -> None:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self._body.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{width}"{dash_attr}/>'
        )

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: float = 14,
        fill: str = INK,
        anchor: str = "start",
        weight: str = "normal",
        family: str = UI,
        opacity: float | None = None,
    ) -> None:
        extra = f' opacity="{opacity}"' if opacity is not None else ""
        self._body.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}"{extra}>'
            f"{esc(value)}</text>"
        )

    def text_block(
        self,
        x: float,
        y: float,
        lines: list[str],
        size: float = 13,
        fill: str = INK,
        anchor: str = "start",
        weight: str = "normal",
        family: str = UI,
        leading: float = 20,
    ) -> None:
        for index, value in enumerate(lines):
            self.text(x, y + index * leading, value, size, fill, anchor, weight, family)

    # -- 组合件 -----------------------------------------------------------

    def band(self, x: float, y: float, w: float, h: float, color: str, bg: str) -> None:
        self.rect(x, y, w, h, bg, color, rx=14, sw=1.2, opacity=0.62)
        self.rect(x, y, 5, h, color, None, rx=2.5, opacity=0.9)

    def card(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        title: str,
        lines: list[str] | None = None,
        color: str = SLATE,
        bg: str = WHITE,
        title_size: float = 14,
        line_size: float = 12,
        mono: bool = False,
    ) -> None:
        self.rect(x, y, w, h, bg, color, rx=8, sw=1.1)
        body = lines or []
        total = title_size + (len(body) * (line_size + 5) if body else 0)
        top = y + (h - total) / 2 + title_size * 0.82
        self.text(x + 12, top, title, title_size, color, "start", "bold", MONO if mono else UI)
        for index, value in enumerate(body):
            self.text(
                x + 12,
                top + (index + 1) * (line_size + 5) + 2,
                value,
                line_size,
                MUTED,
                "start",
                "normal",
                MONO if mono else UI,
            )

    def chip(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str,
        color: str,
        bg: str = WHITE,
        size: float = 13,
        mono: bool = False,
    ) -> None:
        self.rect(x, y, w, h, bg, color, rx=h / 2, sw=1.1)
        self.text(x + w / 2, y + h / 2 + size * 0.36, label, size, color, "middle", "normal", MONO if mono else UI)

    def arrow(
        self,
        points: list[tuple[float, float]],
        color: str = SLATE,
        width: float = 1.6,
        dash: str | None = None,
        head: bool = True,
        head_len: float = 11.0,
        head_half: float = 4.4,
    ) -> None:
        # Changed 2026-10-18: Qt 的 QSvgRenderer 对 <marker> 的 refX/orient 支持不完整，
        # 渲染出来箭头会画歪，所以不用 marker，直接按末段方向手画三角形。
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self._body.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
        )
        if not head or len(points) < 2:
            return
        (x1, y1), (x2, y2) = points[-2], points[-1]
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            return
        # 短线段上头太大会显得突兀，按线长自适应收缩。
        if length < head_len * 1.6:
            head_len = max(6.0, length * 0.62)
            head_half = head_len * 0.42
        ux, uy = dx / length, dy / length          # 指向箭头尖的单位向量
        bx, by = x2 - ux * head_len, y2 - uy * head_len
        px, py = -uy * head_half, ux * head_half
        self._body.append(
            f'<path d="M{x2:.1f},{y2:.1f} L{bx + px:.1f},{by + py:.1f} '
            f'L{bx - px:.1f},{by - py:.1f} Z" fill="{color}"/>'
        )

    def arrow_label(
        self,
        x: float,
        y: float,
        value: str,
        color: str = SLATE,
        size: float = 12.5,
        anchor: str = "middle",
        family: str = UI,
        weight: str = "normal",
        pad: float = 5,
    ) -> None:
        width = text_width(value, size)
        if anchor == "middle":
            left = x - width / 2 - pad
        elif anchor == "end":
            left = x - width - pad
        else:
            left = x - pad
        self.rect(left, y - size - 2, width + pad * 2, size + 9, WHITE, None, rx=4, opacity=0.94)
        self.text(x, y, value, size, color, anchor, weight, family)

    def title(self, value: str, subtitle: str | None = None) -> None:
        self.text(44, 44, value, 21, INK, "start", "bold")
        if subtitle:
            self.text(44, 68, subtitle, 12.5, MUTED)

    def dump(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
            f'height="{self.height}" viewBox="0 0 {self.width} {self.height}">'
            f'<rect width="{self.width}" height="{self.height}" fill="{self.bg}"/>'
            f"{''.join(self._body)}</svg>\n"
        )


# ---------------------------------------------------------------- 通用绘制


def lifeline(svg: Svg, x: float, top: float, bottom: float, label: str, sub: str, color: str, bg: str) -> None:
    svg.rect(x - 108, top, 216, 50, bg, color, rx=9, sw=1.4)
    svg.text(x, top + 22, label, 14, color, "middle", "bold")
    svg.text(x, top + 39, sub, 11.5, MUTED, "middle")
    svg.line(x, top + 50, x, bottom, LINE, 1.2, "5 6")


def message(
    svg: Svg,
    x1: float,
    x2: float,
    y: float,
    label: str,
    color: str,
    dash: str | None = None,
    size: float = 12.5,
    above: bool = True,
) -> None:
    svg.arrow([(x1, y), (x2, y)], color, 1.7, dash)
    mid = (x1 + x2) / 2
    svg.arrow_label(mid, y - 7 if above else y + 18, label, color, size)


def self_call(svg: Svg, x: float, y: float, label: str, color: str, width: float = 56) -> float:
    svg.arrow([(x, y), (x + width, y), (x + width, y + 26), (x + 2, y + 26)], color, 1.5)
    svg.text(x + width + 10, y + 15, label, 12.5, color)
    return y + 26


# ---------------------------------------------------------------- 图 1 架构


def diagram_architecture() -> str:
    svg = Svg(1200, 720)
    svg.title(
        "图 1 · 系统分层：一台上位机 + 一块板子 + 一根 USB 线",
        "上位机不定义任何私有图像帧格式，全部通信都跑在 OpenMV 固件自带的 CDC 二进制协议上。",
    )

    # --- 上位机 --------------------------------------------------------
    svg.band(40, 84, 1120, 300, BLUE, BLUE_BG)
    svg.text(58, 108, "上位机进程 · Python 3.11 + PySide6", 12.5, BLUE, "start", "bold")

    svg.text(58, 134, "应用层", 11.5, MUTED)
    app = [
        ("MainWindow", ["交互编排 / 日志 / 主题"]),
        ("VideoDisplay", ["叠加绿框与红框 / 点选"]),
        ("SuspectLock", ["嫌疑人身份保持"]),
        ("VideoRecorder", ["mp4v 录像"]),
    ]
    for index, (name, lines) in enumerate(app):
        svg.card(58 + index * 273, 142, 261, 56, name, lines, BLUE, WHITE, 13, 11.5)

    svg.text(58, 222, "协议层", 11.5, MUTED)
    svg.card(58, 230, 528, 62, "OpenMVSerialVideoSource", ["Qt 信号桥：取帧线程 → UI 线程"], PURPLE, WHITE, 13, 11.5)
    svg.card(598, 230, 544, 62, "OpenMVProtocolClient", ["CRC / 组包 / 握手 / 通道命令 / 读帧事务"], PURPLE, WHITE, 13, 11.5)

    svg.rect(58, 302, 1084, 26, WHITE, PURPLE, rx=6, sw=1, dash="4 4")
    svg.text(70, 320, "取帧线程 _OpenMVFrameReader：读 stream 通道 · 每 50 ms 轮询 stdout · 3/6/9 s 无帧看门狗自愈", 12, PURPLE)

    svg.text(58, 350, "物理层", 11.5, MUTED)
    svg.card(58, 358, 1084, 18, "", [], BLUE, WHITE)
    svg.text(70, 371, "pyserial.Serial(COMx, 921600, timeout=2.0)  →  USB CDC  →  板载 runtime VCP（VID:PID 37C5:16E3）", 12, BLUE, "start", "normal", MONO)

    # --- USB 边界 ------------------------------------------------------
    svg.line(40, 420, 1160, 420, LINE, 2, "10 6")
    svg.arrow_label(600, 420, "同一根 USB：stdin 收脚本 · stream 出帧 · stdout 出文本（唯一协议：OpenMV CDC）", SLATE, 12.5, "middle", UI, "bold")

    # --- 设备端 --------------------------------------------------------
    svg.band(40, 450, 1120, 230, GREEN, GREEN_BG)
    svg.text(58, 474, "设备端 · RA8P1 Titan Mini · MicroPython v1.28.0-openmv", 12.5, GREEN, "start", "bold")

    channels = [
        ("stdin", "收脚本（RAM 执行，不落盘）"),
        ("stream", "出帧：20 B 帧头 + JPEG"),
        ("stdout", "出文本：print(JSON 检测结果)"),
    ]
    for index, (name, desc) in enumerate(channels):
        x = 58 + index * 362
        svg.rect(x, 488, 348, 52, WHITE, GREEN, rx=8, sw=1.1)
        svg.chip(x + 12, 500, 92, 28, name, GREEN, GREEN_BG, 12.5, mono=True)
        svg.text(x + 118, 519, desc, 12, MUTED)

    svg.card(
        58,
        558,
        1084,
        100,
        "examples/openmv_ae3_raw_jpeg.py —— 设备端脚本（唯一真源，IDE 与上位机跑同一份文本）",
        [],
        GREEN,
        WHITE,
        13,
    )
    steps = [
        ("csi0.snapshot()", "RGB565 / QVGA"),
        ("ml.predict()", "每 2 帧推理一次"),
        ("IoU 跟踪器", "设备内 track_id"),
        ("print(JSON)", "tracks_update"),
    ]
    for index, (name, desc) in enumerate(steps):
        x = 72 + index * 268
        svg.rect(x, 592, 252, 52, GREEN_BG, GREEN, rx=8, sw=1)
        svg.text(x + 14, 613, name, 12.5, GREEN, "start", "bold", MONO)
        svg.text(x + 14, 632, desc, 11.5, MUTED)
        if index < len(steps) - 1:
            svg.arrow([(x + 254, 618), (x + 266, 618)], GREEN, 1.4)

    svg.text(44, 704, "OpenMV IDE 连的也是同一根线、同一套协议（见图 7）；设备端不存在第二套私有帧格式。", 12, MUTED)
    return svg.dump()


# ---------------------------------------------------------------- 图 2 一帧的旅程


def diagram_frame_journey() -> str:
    svg = Svg(1200, 800)
    svg.title(
        "图 2 · 一帧的完整旅程：一次读帧 = 四次命令往返",
        "顺序固定为 LOCK → SIZE → READ → UNLOCK，整段必须原子完成；帧总是先落在设备的 stream 通道里等上位机来取。",
    )

    x_script, x_fw, x_proto, x_ui = 150, 460, 790, 1060
    top = 88
    lifeline(svg, x_script, top, 700, "设备脚本", "openmv_ae3_raw_jpeg.py", GREEN, GREEN_BG)
    lifeline(svg, x_fw, top, 700, "固件 stream 通道", "帧缓冲 + 分片", GREEN, WHITE)
    lifeline(svg, x_proto, top, 700, "上位机协议层", "OpenMVProtocolClient", PURPLE, PURPLE_BG)
    lifeline(svg, x_ui, top, 700, "UI 线程", "MainWindow", BLUE, BLUE_BG)

    message(svg, x_script, x_fw, 178, "csi0.snapshot() → JPEG 压入 stream 缓冲", GREEN, size=12)

    message(svg, x_proto, x_fw, 224, "① CHANNEL_LOCK(0x22)", PURPLE, size=12)
    message(svg, x_fw, x_proto, 262, "ACK  或  NAK 0x04 = BUSY（本轮无帧，直接返回 None）", GREEN, "5 5", 11.5)
    message(svg, x_proto, x_fw, 304, "② CHANNEL_SIZE(0x25)", PURPLE, size=12)
    message(svg, x_fw, x_proto, 342, "<I> 帧总长 ≈ 17 KB", GREEN, "5 5", 12)
    message(svg, x_proto, x_fw, 384, "③ CHANNEL_READ(0x26) offset=0, length=size", PURPLE, size=12)
    message(svg, x_fw, x_proto, 422, "20 B 帧头 + JPEG（按 4082 分 5 片，每片独立 CRC32）", GREEN, "5 5", 11.5)
    message(svg, x_proto, x_fw, 464, "④ CHANNEL_UNLOCK(0x23)", PURPLE, size=12)

    y = self_call(svg, x_proto, 496, "解码 JPEG → uint8 H×W×3 BGR", PURPLE)
    y = self_call(svg, x_proto, y + 20, "附上最近一次 detections（可能为 None）", PURPLE)

    message(svg, x_proto, x_ui, y + 34, "frame_received(image, detections) · Qt 信号", BLUE, size=12)

    svg.rect(950, y + 52, 236, 84, WHITE, BLUE, rx=8, sw=1.1)
    svg.text_block(
        962,
        y + 74,
        ["SuspectLock.update()", "annotate_frame()", "VideoRecorder.write()", "VideoDisplay 绘制"],
        11.5,
        BLUE,
        leading=17,
    )

    svg.rect(44, 716, 1112, 72, SLATE_BG, LINE, rx=10, sw=1)
    svg.text(60, 740, "为什么要 LOCK", 12.5, SLATE, "start", "bold")
    svg.text(
        60,
        759,
        "stream 是共享的单生产者/单消费者通道：不加锁时 UI 线程的 STREAM_CTRL 可能插进事务中间，设备侧锁状态与上位机假设错位，",
        12,
        MUTED,
    )
    svg.text(60, 778, "表现为「停流后再启流收不到帧」。整段包在 threading.RLock 里，异常路径靠 finally 解锁。", 12, MUTED)
    return svg.dump()


# ---------------------------------------------------------------- 图 3 帧格式


def diagram_cdc_frame() -> str:
    svg = Svg(1200, 500)
    svg.title(
        "图 3 · OpenMV CDC 包格式（10 字节头 + payload + CRC32）",
        "多字节整数一律 little-endian；空 payload 的包不带 CRC32 —— 读侧按 length ? +4 : +0 计算包长。",
    )

    unit = 55
    x0 = 64
    y0 = 130
    height = 76
    fields = [
        ("sync", 2, "0xD5AA", "AA D5"),
        ("seq", 1, "u8", "回绕"),
        ("chan", 1, "u8", "0=系统"),
        ("flags", 1, "u8", "6 位"),
        ("opcode", 1, "u8", "命令号"),
        ("length", 2, "u16", "字节数"),
        ("header_crc16", 2, "u16", "0..7"),
    ]
    x = x0
    palette = [BLUE, BLUE, BLUE, BLUE, BLUE, BLUE, PURPLE]
    for (name, size, type_name, note), color in zip(fields, palette):
        w = size * unit
        svg.rect(x, y0, w, height, WHITE, color, rx=6, sw=1.4)
        svg.text(x + w / 2, y0 + 26, name, 12.5, color, "middle", "bold")
        svg.text(x + w / 2, y0 + 45, type_name, 11.5, MUTED, "middle", "normal", MONO)
        svg.text(x + w / 2, y0 + 62, note, 10.5, FAINT, "middle")
        x += w

    head_end = x
    payload_w = 330
    svg.rect(x, y0, payload_w, height, ORANGE_BG, ORANGE, rx=6, sw=1.4)
    svg.text(x + payload_w / 2, y0 + 26, "payload", 12.5, ORANGE, "middle", "bold")
    svg.text(x + payload_w / 2, y0 + 45, "length 字节", 11.5, MUTED, "middle")
    svg.text(x + payload_w / 2, y0 + 62, "脚本 / JPEG / 通道数据", 10.5, FAINT, "middle")
    x += payload_w + 6

    svg.rect(x, y0, 4 * unit, height, ORANGE_BG, RED, rx=6, sw=1.4)
    svg.text(x + 2 * unit, y0 + 26, "payload_crc32", 12, RED, "middle", "bold")
    svg.text(x + 2 * unit, y0 + 45, "u32", 11.5, MUTED, "middle", "normal", MONO)
    svg.text(x + 2 * unit, y0 + 62, "仅 length > 0", 10.5, FAINT, "middle")

    # 偏移刻度
    offsets = [0, 2, 3, 4, 5, 6, 8, 10]
    acc = 0
    scale = [0, 2, 1, 1, 1, 1, 2, 2]
    tick_x = x0
    for off, span in zip(offsets, scale):
        svg.line(tick_x, y0 - 6, tick_x, y0, FAINT, 1)
        svg.text(tick_x, y0 - 12, str(off), 11, FAINT, "middle", "normal", MONO)
        tick_x += span * unit
    svg.line(tick_x, y0 - 6, tick_x, y0, FAINT, 1)
    svg.text(tick_x, y0 - 12, "10", 11, FAINT, "middle", "normal", MONO)

    # CRC 覆盖范围
    svg.arrow([(x0, y0 + height + 26), (x0, y0 + height + 10)], PURPLE, 1.2, head=False)
    svg.arrow([(x0 + 8 * unit, y0 + height + 26), (x0 + 8 * unit, y0 + height + 10)], PURPLE, 1.2, head=False)
    svg.line(x0, y0 + height + 26, x0 + 8 * unit, y0 + height + 26, PURPLE, 1.4)
    svg.text(x0 + 4 * unit, y0 + height + 46, "CRC16 覆盖头前 8 字节（多项式 0xF94F，初值 0xFFFF）", 12, PURPLE, "middle")

    svg.arrow([(head_end, y0 + height + 26), (head_end, y0 + height + 10)], ORANGE, 1.2, head=False)
    svg.arrow([(head_end + payload_w, y0 + height + 26), (head_end + payload_w, y0 + height + 10)], ORANGE, 1.2, head=False)
    svg.line(head_end, y0 + height + 26, head_end + payload_w, y0 + height + 26, ORANGE, 1.4)
    svg.text(head_end + payload_w / 2, y0 + height + 46, "CRC32 覆盖 payload（0xFA567D89）", 12, ORANGE, "middle")

    # flags 位
    svg.text(64, 300, "flags 位定义", 13, INK, "start", "bold")
    bits = [
        ("bit0", "ACK", "成功确认"),
        ("bit1", "NAK", "前 2 字节 = 状态码"),
        ("bit2", "RTX", "重传；错序号回 ACK 后丢弃"),
        ("bit3", "ACK_REQ", "必须回一个空 ACK"),
        ("bit4", "FRAGMENT", "还有后续分片，延长超时"),
        ("bit5", "EVENT", "异步事件，不占用序号"),
    ]
    for index, (bit, name, note) in enumerate(bits):
        x = 64 + (index % 3) * 372
        y = 318 + (index // 3) * 62
        svg.rect(x, y, 352, 52, WHITE, LINE, rx=8, sw=1)
        svg.text(x + 14, y + 21, bit, 11.5, FAINT, "start", "normal", MONO)
        svg.text(x + 60, y + 21, name, 12.5, SLATE, "start", "bold", MONO)
        svg.text(x + 14, y + 40, note, 11.5, MUTED)

    svg.rect(64, 448, 1072, 34, SLATE_BG, LINE, rx=8, sw=1)
    svg.text(78, 470, "注意：头用 CRC16 挡假同步字，体用 CRC32 保证「这不是半张图」。两者都是 MSB-first 查表，不能用 zlib.crc32 顶替。", 12, SLATE)
    return svg.dump()


# ---------------------------------------------------------------- 图 4 握手


def diagram_handshake() -> str:
    svg = Svg(1200, 710)
    svg.title(
        "图 4 · 连接握手：四步完成能力协商，payload 上限从 52 B 提到 4082 B",
        "这四步就是 OpenMV IDE 连接时做同样的事；上位机只是把它的一个子集重写了一遍。",
    )

    x_host, x_dev = 400, 880
    top = 88
    lifeline(svg, x_host, top, 640, "上位机", "OpenMVProtocolClient.connect()", PURPLE, PURPLE_BG)
    lifeline(svg, x_dev, top, 640, "设备固件", "OpenMV runtime", GREEN, GREEN_BG)

    message(svg, x_host, x_dev, 186, "PROTO_SYNC(0x00) · payload 上限此时只有 52 B", PURPLE, size=12)
    message(svg, x_dev, x_host, 224, "ACK · 双方序号清零", GREEN, "5 5", 12)

    message(svg, x_host, x_dev, 272, "PROTO_GET_CAPS(0x01)", PURPLE, size=12)
    message(svg, x_dev, x_host, 310, "<IH> flags, device_max_payload = 4082", GREEN, "5 5", 12)

    message(svg, x_host, x_dev, 358, "PROTO_SET_CAPS(0x02) flags=0x0F, max_payload=4082", PURPLE, size=12)
    message(svg, x_dev, x_host, 396, "ACK", GREEN, "5 5", 12)

    message(svg, x_host, x_dev, 444, "CHANNEL_LIST(0x20)", PURPLE, size=12)
    message(svg, x_dev, x_host, 482, "每项 16 B：<BB14s> id / flags / name", GREEN, "5 5", 12)

    y = self_call(svg, x_host, 524, "校验 stdin + stream 必须存在", PURPLE)
    y = self_call(svg, x_host, y + 18, "wait_for_channel('tracker') 1.5 s → 失败也继续", PURPLE)

    svg.rect(44, 96, 208, 96, WHITE, PURPLE, rx=8, sw=1)
    svg.text_block(
        58,
        118,
        ["SYNC 失败重试 3 次", "每次重试前：", "reset_input_buffer()", "+ 清空解析缓冲"],
        11.5,
        PURPLE,
        leading=18,
    )

    svg.rect(44, 616, 1112, 76, SLATE_BG, LINE, rx=10, sw=1)
    svg.text(60, 640, "协商结果直接决定后面所有命令的分片大小", 12.5, SLATE, "start", "bold")
    svg.text(
        60,
        662,
        "max_payload = min(4096, 设备上报 4082) = 4082 → 一次 CHANNEL_WRITE 只能带 4082-8 = 4074 字节脚本，"
        "一帧 17 KB 的 JPEG 必然切成 5 片。",
        12,
        MUTED,
    )
    svg.text(60, 682, "把 SET_CAPS 里的数字调大没有用：4082 是固件侧的硬上限（已实测）。", 12, MUTED)
    return svg.dump()


# ---------------------------------------------------------------- 图 5 读帧事务


def diagram_transaction() -> str:
    svg = Svg(1200, 470)
    svg.title(
        "图 5 · 读帧事务的时间预算：四次往返 ≈ 22 ms，这是帧率上限的硬下限",
        "LOCK 的响应延迟（设备端要等下一次调度）占了三分之一，无法靠改上位机代码消掉。",
    )

    scale = 48.0
    x = 84
    y = 150
    segments = [
        ("CHANNEL_LOCK", 8.0, PURPLE, "含设备响应延迟"),
        ("SIZE", 1.7, BLUE, ""),
        ("CHANNEL_READ", 10.0, ORANGE, "5 分片 + CRC32 校验"),
        ("UNLOCK", 2.3, GREEN, ""),
    ]
    for name, ms, color, note in segments:
        w = ms * scale
        svg.rect(x, y, w, 58, WHITE, color, rx=4, sw=1.4)
        svg.text(x + w / 2, y + 25, name, 12, color, "middle", "bold", MONO)
        svg.text(x + w / 2, y + 44, f"{ms:g} ms", 11.5, MUTED, "middle", "normal", MONO)
        if note:
            svg.text(x + w / 2, y + 80, note, 11, FAINT, "middle")
        x += w

    total = x - 84
    svg.line(84, y + 104, 84 + total, y + 104, SLATE, 1.4)
    svg.line(84, y + 98, 84, y + 110, SLATE, 1.4)
    svg.line(84 + total, y + 98, 84 + total, y + 110, SLATE, 1.4)
    svg.text(84 + total / 2, y + 124, "一帧 ≈ 22 ms → 单线程理论上限 ≈ 45 fps（实测带检测 16~19 fps）", 12.5, SLATE, "middle", "bold")

    # RLock 包裹
    svg.arrow([(84, y - 14), (84, y - 4)], PURPLE, 1.2, head=False)
    svg.arrow([(84 + total, y - 14), (84 + total, y - 4)], PURPLE, 1.2, head=False)
    svg.line(84, y - 14, 84 + total, y - 14, PURPLE, 1.4)
    svg.text(84 + total / 2, y - 20, "threading.RLock 覆盖整段（UI 线程的 STREAM_CTRL 不能插进来）", 12, PURPLE, "middle")

    # 错误示范
    svg.rect(44, 320, 1112, 118, RED_BG, RED, rx=10, sw=1.2, opacity=0.8)
    svg.text(62, 346, "反例：把 set_streaming(False) 插进事务中间", 13, RED, "start", "bold")

    y2 = 372
    x2 = 62
    bad = [("LOCK", 150, PURPLE), ("SIZE", 110, BLUE), ("STREAM_CTRL 插入", 190, RED), ("READ", 200, ORANGE), ("UNLOCK", 130, GREEN)]
    for name, w, color in bad:
        svg.rect(x2, y2, w, 40, WHITE, color, rx=4, sw=1.3, dash="6 4" if color == RED else None)
        svg.text(x2 + w / 2, y2 + 25, name, 11.5, color, "middle", "bold", MONO if color != RED else UI)
        x2 += w
    svg.text(62, y2 + 62, "设备侧把 LOCK 释放掉 + 停流，上位机却仍以为自己在事务里 → 之后每一次 READ 都拿到空数据，表现为「停流后再启流永远黑屏」。", 12, MUTED)
    return svg.dump()


# ---------------------------------------------------------------- 图 6 双通道


def diagram_two_channels() -> str:
    svg = Svg(1200, 620)
    svg.title(
        "图 6 · 两条数据链：帧走 stream（上位机来取），检测走 stdout（设备主动说）",
        "两者速率与相位都不同步，所以「有画面」和「有检测框」是两件独立的事。",
    )

    # 左：stream
    svg.band(44, 84, 552, 330, ORANGE, ORANGE_BG)
    svg.text(62, 108, "① 图像链 · stream 通道", 13, ORANGE, "start", "bold")
    left_steps = [
        ("固件把 snapshot 压成 JPEG", "帧就绪后停在通道缓冲里"),
        ("上位机每帧 4 次命令取走", "LOCK / SIZE / READ / UNLOCK ≈22 ms"),
        ("解码成 BGR ndarray", "uint8 H×W×3，C-contiguous"),
        ("frame_received(image, …)", "Qt 信号 → UI 线程"),
    ]
    for index, (name, desc) in enumerate(left_steps):
        y = 122 + index * 70
        svg.rect(62, y, 516, 56, WHITE, ORANGE, rx=8, sw=1)
        svg.text(76, y + 23, name, 12.5, ORANGE, "start", "bold")
        svg.text(76, y + 42, desc, 11.5, MUTED)
        if index < len(left_steps) - 1:
            svg.arrow([(320, y + 59), (320, y + 68)], ORANGE, 1.5)

    # 右：stdout
    svg.band(604, 84, 552, 330, CYAN, CYAN_BG)
    svg.text(622, 108, "② 检测链 · stdout 通道", 13, CYAN, "start", "bold")
    right_steps = [
        ("脚本 print(JSON 一行 ~180 B)", "tracks_update + frame_size + fps"),
        ("进 stdout 环形缓冲", "上位机不读会反压，拖慢采集循环"),
        ("上位机每 50 ms 轮询一次", "SIZE / READ('stdout')"),
        ("解析成 _latest_detections", "非 JSON 行（横幅/栈）直接跳过"),
    ]
    for index, (name, desc) in enumerate(right_steps):
        y = 122 + index * 70
        color = RED if index == 1 else CYAN
        svg.rect(622, y, 516, 56, WHITE, color, rx=8, sw=1)
        svg.text(636, y + 23, name, 12.5, color, "start", "bold")
        svg.text(636, y + 42, desc, 11.5, MUTED)
        if index < len(right_steps) - 1:
            svg.arrow([(880, y + 59), (880, y + 68)], color, 1.5)

    # 汇合：两条链各自拐进 frame_received 框的顶部，不在中途相交
    svg.arrow([(300, 418), (300, 447), (540, 447), (540, 468)], ORANGE, 1.7)
    svg.arrow([(860, 418), (860, 447), (660, 447), (660, 468)], CYAN, 1.7)

    svg.rect(300, 470, 600, 74, WHITE, SLATE, rx=10, sw=1.4)
    svg.text(600, 497, "frame_received(image, detections)", 13.5, SLATE, "middle", "bold", MONO)
    svg.text(600, 518, "最近一次检测被「贴」到当前帧上一起交给 UI", 11.5, MUTED, "middle")
    svg.text(600, 536, "两者不是同一时刻的数据", 11.5, RED, "middle")

    svg.rect(44, 562, 1112, 44, SLATE_BG, LINE, rx=8, sw=1)
    svg.text(
        60,
        589,
        "所以 detections 有第四种状态：None = 设备还没上报过（画面里可能有框也可能没有），[] = 明确上报过「当前没人」。两者在 UI 上必须区别对待。",
        12,
        SLATE,
    )
    return svg.dump()


# ---------------------------------------------------------------- 图 7 同协议对接


def diagram_ide() -> str:
    svg = Svg(1200, 690)
    svg.title(
        "图 7 · 与 OpenMV IDE 的关系：同一套协议、同一个 COM 口、同一份脚本",
        "上位机不是「另立协议」，而是按官方 wire format 重写的一个最小子集 —— 能和 IDE 在同一根线上说同一种话。",
    )

    # IDE
    svg.rect(44, 94, 400, 250, WHITE, BLUE, rx=12, sw=1.4)
    svg.rect(44, 94, 400, 34, BLUE_BG, BLUE, rx=12, sw=1.4)
    svg.text(60, 116, "OpenMV IDE", 13.5, BLUE, "start", "bold")
    svg.text_block(
        60,
        152,
        ["· 官方 C++ 实现，全量协议", "· SYNC / CAPS / 全部命令 / profiler", "· 脚本保存时写设备 SD 卡", "· 自带帧缓冲窗口看画面", "· 调试体验最好"],
        12,
        MUTED,
        leading=24,
    )

    # 本工程
    svg.rect(756, 94, 400, 250, WHITE, PURPLE, rx=12, sw=1.4)
    svg.rect(756, 94, 400, 34, PURPLE_BG, PURPLE, rx=12, sw=1.4)
    svg.text(772, 116, "本工程上位机", 13.5, PURPLE, "start", "bold")
    svg.text_block(
        772,
        152,
        ["· Python 手写的最小可用子集", "· 只实现 SYNC/CAPS + 通道 6 条命令", "· 脚本只写 stdin，RAM 执行不落盘", "· 自己解码 stream 通道出图", "· 面向现场操作，无调试界面"],
        12,
        MUTED,
        leading=24,
    )

    # 中间：同一套 wire protocol
    svg.rect(484, 94, 232, 250, SLATE_BG, SLATE, rx=12, sw=1.4)
    svg.text(600, 122, "同一套 wire format", 13, SLATE, "middle", "bold")
    svg.text(600, 142, "10 字节头 + payload", 11, MUTED, "middle")
    strip = ["sync", "seq", "ch", "fl", "op", "len", "crc16"]
    for index, name in enumerate(strip):
        svg.rect(494, 156 + index * 22, 212, 18, WHITE, SLATE, rx=3, sw=0.9)
        svg.text(506, 169 + index * 22, name, 10.5, SLATE, "start", "normal", MONO)
        if index == 6:
            svg.text(694, 169 + index * 22, "→", 10, FAINT, "middle")

    svg.arrow([(444, 210), (484, 210)], BLUE, 2)
    svg.arrow([(756, 210), (716, 210)], PURPLE, 2)

    # 板子
    svg.rect(340, 384, 520, 92, GREEN_BG, GREEN, rx=12, sw=1.4)
    svg.text(600, 412, "RA8P1 Titan Mini · 同一份固件", 13.5, GREEN, "middle", "bold")
    svg.text(600, 436, "USB CDC 枚举为 COM 口（VID:PID 37C5:16E3）", 12, MUTED, "middle")
    svg.text(600, 458, "同一时刻只能有一个程序打开它", 12, RED, "middle", "bold")

    svg.arrow([(244, 344), (400, 384)], BLUE, 1.6, "6 5")
    svg.arrow([(956, 344), (800, 384)], PURPLE, 1.6, "6 5")

    # 互斥说明
    svg.rect(44, 500, 1112, 90, WHITE, RED, rx=10, sw=1.2, opacity=0.9)
    svg.text(62, 526, "互斥与切换（现场最常踩的坑）", 13, RED, "start", "bold")
    svg.text(
        62,
        550,
        "IDE 连着 → 上位机打开 COM 失败 / PROTO_SYNC 三次全超时；上位机连着 → IDE 报连接失败。切换顺序：IDE 点停止 → 断开 IDE 连接 → 上位机连接。",
        12,
        MUTED,
    )
    svg.text(62, 574, "反过来：上位机断开（会自动 STDIN_STOP）→ IDE 连接。任何时刻只能有一个「主人」。", 12, MUTED)

    svg.text(
        44,
        626,
        "为什么不用官方 Python SDK：较新版本要求的 Python 高于本工程的 3.11；子集足够跑通「取帧 + 传脚本」，也不用跟着 IDE 的版本升级走。",
        12,
        MUTED,
    )
    svg.text(44, 652, "代价：IDE 能用的统计/配置/自定义通道，上位机没有 —— 遇到板端问题时仍应回 IDE 排查。", 12, MUTED)
    return svg.dump()


# ---------------------------------------------------------------- 入口

DIAGRAMS = {
    "01_architecture": diagram_architecture,
    "02_frame_journey": diagram_frame_journey,
    "03_cdc_frame_format": diagram_cdc_frame,
    "04_handshake": diagram_handshake,
    "05_frame_transaction": diagram_transaction,
    "06_two_channels": diagram_two_channels,
    "07_ide_same_protocol": diagram_ide,
}


def main() -> int:
    out_dir = Path(__file__).resolve().parent / "src"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, builder in DIAGRAMS.items():
        path = out_dir / f"{name}.svg"
        path.write_text(builder(), encoding="utf-8")
        print(f"wrote {path.relative_to(out_dir.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
