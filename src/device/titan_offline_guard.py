# Titan Mini OFFLINE GUARD page -- the application the board runs when the host is away.
#
# Two ways into this script, one single file:
#   * SD autostart -- /main.py is this file, so a power cycle boots straight into guard mode.
#   * host disconnect -- the PC uploads this same text right before closing the serial port,
#     so clicking "disconnect" hands the board over to guard mode immediately.
# The host's online page (titan_lcd_monitor.py) stops this script on the next connect; the
# two pages never run at the same time.
#
# Differences from the online page:
#   * different layout -- the header strip reads 离线监控, the video bar shows a REC pill,
#     and the status column carries 录像/事件 rows instead of 上屏/欠载 (those only make
#     sense with a host watching the scanout).
#   * event recording -- the annotated camera frame is written to /tracker/rec as an MJPEG
#     segment while a target is present; the segment closes shortly after the target leaves
#     and rolls to a new file every 5 minutes.
#   * target lock -- offline there is nobody to point at the suspect, so the page infers it:
#     the largest (nearest) person becomes the target, gets the red box and is the only one
#     burned into the recording. Changed 2026-10-20: the FT5406 panel does not answer on
#     this board (docs/ft5406_touch_driver.md), so the operator control moved to the board's
#     own USER_KEY (P110): single press re-arms and re-locks, double press cycles the target,
#     long press clears the lock and records everybody. A segment used to hold the guard for
#     a 30 s touch wait, during which nobody was recorded at all -- that blind spot is gone:
#     the guard re-arms itself 1.5 s after a segment closes.
#   * event log -- boot, recording and person-count changes are appended to /tracker/log in
#     buffered writes (a FAT append costs 20-100 ms on this card, so lines are batched).
#
# The panel timing and the half-size canvas rules are identical to the online page: the
# canvas is 400x240 scaled 2x on display (an 800x480 canvas steals SDRAM from the NPU and
# wrecks the frame budget -- see titan_lcd_monitor.py), and board.preview(False) is still
# forbidden: it would switch off the framebuffer the host stream channel reads from, which
# breaks the *next* host session until the board reboots.

import csi
import gc
import image
import lcd
import machine
import ml
import mjpeg
import os
import struct
import time
import ujson
from ml.postprocessing.ultralytics import YoloV8

BUILD_TAG = "g2026-10-24a"

# The offline page is Chinese by definition: it runs without an operator looking at the PC.
PAGE_LANGUAGE = "zh"

PAGE_TEXT = {
    "zh": {
        # Changed 2026-10-18h: 守卫 -> 监控 (user-facing wording; the module name stays
        # titan_offline_guard.py so the host upload path and SD copy do not move).
        "header": "离线监控", "state": "状态", "guarding": "监控中", "waiting": "等待",
        "persons": "人数", "capture": "采集", "infer": "推理", "record": "录像",
        "events": "事件", "offline": "离线", "boot": "启动中",
        # Changed 2026-10-18b: seventh row shows the recording footprint against the card,
        # so the 70% recycle policy is visible on the panel instead of log-only.
        "sd": "存储",
        # Changed 2026-10-20: the state row now says what the target logic is doing. 已锁定
        # is the normal auto-lock mode; 全录 means the operator cleared the lock with a long
        # press, so bystanders are recorded too.
        "locking": "已锁定", "allrec": "全录",
        # Changed 2026-10-20d: LVGL redesign status card line (target / fallback count).
        "target": "目标",
    },
    "en": {
        "header": "MONITOR", "state": "STATE", "guarding": "ACTIVE", "waiting": "WAIT",
        "persons": "PERSONS", "capture": "CAP FPS", "infer": "INFER", "record": "REC",
        "events": "EVENTS", "offline": "OFFLINE", "boot": "LOADING",
        "sd": "SD",
        "locking": "LOCKED", "allrec": "ALL",
        # TGT instead of TARGET: the 68 px card cannot fit a 72 px string.
        "target": "TGT",
    },
}
TXT = PAGE_TEXT[PAGE_LANGUAGE if PAGE_LANGUAGE in PAGE_TEXT else "zh"]

MODEL_PATH = "/yolov8n_coco80_192_u55_256.tflite"
SCORE_THRESHOLD = 0.3
PERSON_CLASS = 0
DETECT_EVERY = 2
BACKLIGHT = 85
FRAME_PERIOD_MS = 75

# --- recording policy ----------------------------------------------------------
# A segment opens while the locked target is in frame and closes shortly after it leaves;
# the guard then re-arms itself after REARM_DELAY_MS. Frames are written every REC_EVERYth
# frame, and only on non-inference frames: the write measured 33 ms median / 81 ms peak at
# the default quality, and inference frames are the ones closest to the budget.
# Changed 2026-10-20: writes are now merged with `REC_QUALITY`, which brings the worst case
# down to ~23 ms -- see the measurement table next to REC_QUALITY.
REC_EVERY = 2
# Changed 2026-10-18e (SD protection): a short 3 s debounce so a brief occlusion does not
# close the segment, but walking out of the frame stops it instead of recording 15 s of
# empty scene like the first version did.
REC_STOP_AFTER_EMPTY_S = 3
REC_MAX_SECONDS = 300
# Changed 2026-10-20: the recorder used the module default quality (90), which measured
# 33 ms per write with an 81 ms peak on this card -- over the 75 ms frame budget, which is
# exactly the stutter seen "when a person is detected" (that is when a segment opens and
# the write starts). Measured with src/tests/probe_rec_cost.py, 20 frames each:
#   q90: median 33.3 ms, peak 80.9 ms, 20.7 KB/frame
#   q70: median 21.5 ms, peak 22.7 ms,  9.0 KB/frame   <- fits the budget with margin
#   q50: median 14.7 ms, peak 17.2 ms,  5.4 KB/frame
# 70 keeps enough detail to identify a person at 320x240 and cuts the file to 43%.
REC_QUALITY = 70

# Changed 2026-10-18e (SD protection): once the recordings occupy 70% of the card, the
# oldest segments are deleted before a new one opens, so the guard recycles in place
# instead of filling the card. The card capacity is resolved at boot (see
# read_sd_capacity()): os.statvfs once the firmware enables MICROPY_PY_OS_STATVFS, else
# the per-card override file, else this assumption (logged, never silent).
REC_USAGE_LIMIT = 0.70
SD_TOTAL_ASSUMED = 16 * 1024 * 1024 * 1024

# Operator input.
# Changed 2026-10-20: the FT5406 panel never answers on this board (the I2C path itself
# NACKs -- docs/ft5406_touch_driver.md section 7), so the page can no longer wait for a tap.
# The touch driver stays inlined as a bonus (a tap still re-arms instantly if it ever comes
# alive), but the guard never blocks on it.
#
# The board's own USER_KEY is the replacement and it is free: the SDK's Titan_Mini_key_irq
# example uses BSP_IO_PORT_01_PIN_10 = P110 (input pull-up, press pulls it low), and the
# FSP pin configuration still maps it to IRQ20 (irq.irq20.p110) -- no other code in this
# port touches that pin. No soldering, no firmware change.
#
# The page runs at ~13 fps, so a tap shorter than one frame is invisible: the key has to be
# held for about a fifth of a second for the poller to see it.
KEY_PIN = "P110"
KEY_LONG_MS = 1200        # hold this long, then release -> unlock (record every person)
KEY_DOUBLE_MS = 400       # a second press inside this window cycles the target
REARM_DELAY_MS = 1500     # auto re-arm after a segment closes (was a 30 s touch wait)
LOCK_LOST_MS = 1500       # target gone this long -> the segment ends and a new one picks
                          # the largest person still in frame
TOUCH_RESET_PIN = "P412"
TOUCH_IRQ_PIN = "P007"

# @@INLINE:ft5406.py@@

_touch = None


def touch_init():
    """Bring the capacitive panel up once, after the camera is running.

    Failure is never fatal: the panel is only a convenience here. The guard re-arms itself
    and the USER_KEY carries the operator commands, so an absent controller costs nothing.
    """
    global _touch
    if globals().get("FT5406") is None:
        # Only happens when this file is uploaded raw (dev probes that skip the host
        # composer), i.e. the marker was never replaced.
        log_event("touch driver not inlined in this upload; touch stays disabled")
        return
    try:
        candidate = FT5406(rst=TOUCH_RESET_PIN, irq=TOUCH_IRQ_PIN)
        # Changed 2026-10-19b: 用 diagnose() 而不是 begin()，失败时把写应答/读应答/
        # 复位前后 INT 电平写进日志 —— 这三个值能直接分辨"地址不对"和"芯片没工作"，
        # 现场接线排查全靠它们，不必再跑一遍探针。
        info = candidate.diagnose()
        if info["read_ack"]:
            candidate.configure()
            _touch = candidate
            log_event("touch ready (FT5406 @0x%02X)" % candidate.addr)
        else:
            _touch = None
            log_event("touch silent @0x%02X wr=%s rd=%s int=%s->%s" % (
                candidate.addr, info["write_ack"], info["read_ack"],
                info["irq_before"], info["irq_after"]))
            log_event("touch disabled; USER_KEY and the auto re-arm carry the guard")
    except BaseException as exc:
        _touch = None
        log_event("touch init failed: %s" % exc)


# --- operator input: USER_KEY (P110) -------------------------------------------
# Changed 2026-10-20: replaces the touch screen. The poller runs once per frame (~75 ms),
# which is why every gesture is edge-based and a press must survive at least one frame.
_key_pin = None
_key = {"down": False, "press_ms": 0, "long_fired": False, "last_tap_ms": 0}


def key_init():
    """Configure USER_KEY. Never fatal: a missing key only costs the manual overrides."""
    global _key_pin
    try:
        _key_pin = machine.Pin(KEY_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        # Log the idle level: with the pull-up it must read 1. A 0 here means the pin is
        # driven low elsewhere (wrong board revision) and gestures would fire continuously.
        log_event("user key ready (%s idle=%d)" % (KEY_PIN, _key_pin.value()))
    except BaseException as exc:
        _key_pin = None
        log_event("user key init failed: %s" % exc)


def poll_key(now):
    """Return 'single', 'double' or 'long' for the frame that detected it, else None.

    Changed 2026-10-20: the press now fires 'single' immediately instead of waiting out the
    double-press window, because that 400 ms was the largest part of the "operation feels
    laggy" complaint. A second press inside KEY_DOUBLE_MS still upgrades the gesture to
    'double' -- the first press only arms, so firing it early costs nothing. 'long' fires
    while the key is still held (once), so the operator gets feedback at 1.2 s instead of
    at release.
    """
    if _key_pin is None:
        return None
    pressed = _key_pin.value() == 0
    if pressed:
        if not _key["down"]:
            _key["down"] = True
            _key["press_ms"] = now
            _key["long_fired"] = False
            if _key["last_tap_ms"] and time.ticks_diff(now, _key["last_tap_ms"]) <= KEY_DOUBLE_MS:
                _key["last_tap_ms"] = 0
                return "double"
            _key["last_tap_ms"] = now
            return "single"
        if not _key["long_fired"] and time.ticks_diff(now, _key["press_ms"]) >= KEY_LONG_MS:
            _key["long_fired"] = True
            return "long"
        return None
    _key["down"] = False
    return None


def apply_key(action, tracked, now):
    """Apply one gesture. Safe to call between frames (the idle slice polls the key too)."""
    global _auto_lock, _lock_id, _lock_seen_ms, awaiting_touch
    if action == "single":
        # Arm now. The lock is only re-adopted when there is none (after a long press or at
        # boot) -- a plain confirmation must not yank the target away from the operator.
        awaiting_touch = False
        if not _auto_lock:
            _auto_lock = True
            _lock_id = -1
            _lock_seen_ms = 0
        log_event("key single -> armed")
    elif action == "double":
        _auto_lock = True
        candidate = _next_live(tracked, _lock_id)
        if candidate is not None:
            _lock_id = candidate["id"]
            _lock_seen_ms = now
            log_event("key double -> target %d" % _lock_id)
        else:
            log_event("key double -> nobody in frame")
    elif action == "long":
        _auto_lock = False
        _lock_id = -1
        log_event("key long -> lock cleared (record every person)")


# --- target lock (what the touch screen used to decide) ------------------------
# Offline there is nobody to point at the suspect, so the page infers one: the person with
# the largest box is the nearest one and becomes the target. The operator can override it
# with the key. The lock only decides which box is red and whose segment is recorded --
# detection and the panel keep working with no target at all.
_lock_id = -1
_auto_lock = True
_lock_seen_ms = 0


def _track_area(track):
    box = track["bbox"]
    return (box[2] - box[0]) * (box[3] - box[1])


def _live_tracks(tracked):
    return [track for track in tracked if track["misses"] == 0]


def _largest_live(tracked):
    best = None
    best_area = 0
    for track in _live_tracks(tracked):
        area = _track_area(track)
        if area > best_area:
            best_area = area
            best = track
    return best


def _next_live(tracked, current):
    """Cycle order for the double press: ascending id, wrapping at the end."""
    live = _live_tracks(tracked)
    if not live:
        return None
    live.sort(key=lambda track: track["id"])
    for track in live:
        if track["id"] > current:
            return track
    return live[0]


def lock_step(tracked, now, may_adopt):
    """Return the locked track, or None when there is no target in frame.

    Adopting a new target is gated on ``may_adopt`` (no segment open) so a single MJPEG
    file never mixes two different subjects: the old segment closes first, then the next
    person becomes the target and opens a fresh one.
    """
    global _lock_id, _lock_seen_ms
    if not _auto_lock:
        _lock_id = -1
        return None
    for track in tracked:
        if track["id"] == _lock_id and track["misses"] == 0:
            _lock_seen_ms = now
            return track
    if _lock_id >= 0 and time.ticks_diff(now, _lock_seen_ms) < LOCK_LOST_MS:
        return None
    if not may_adopt:
        return None
    candidate = _largest_live(tracked)
    if candidate is None:
        return None
    if candidate["id"] != _lock_id:
        log_event("target %d -> %d (largest of %d)" % (
            _lock_id, candidate["id"], len(_live_tracks(tracked))))
    _lock_id = candidate["id"]
    _lock_seen_ms = now
    return candidate


def read_touch_confirmed():
    """Touch hook. True once the operator taps the panel to confirm a new suspect."""
    if _touch is None:
        return False
    try:
        return _touch.tapped() is not None
    except BaseException:
        return False

# --- SD storage ----------------------------------------------------------------
HOME = "/tracker"
LOG_DIR = HOME + "/log"
REC_DIR = HOME + "/rec"
# Per-card capacity override: lives on the card itself, so swapping cards means swapping
# the file. Only consulted when os.statvfs is unavailable (see read_sd_capacity).
SD_CAPACITY_FILE = HOME + "/sd_capacity_gb.txt"

CANVAS_W, CANVAS_H = 400, 240
# Changed 2026-10-23d-c: the canvas keeps the online page's geometry but the panel
# present no longer upscales (panel scale 1) -- every pixel including the 320x240
# video is native 1:1. Trade-off: the whole UI is a 400x240 window on the 800x480
# panel (one quarter of the glass, rest dark), and text is half the physical size.
VIDEO_W, VIDEO_H = 320, 240    # on-canvas preview region, 1:1 (source is 320x240)
SRC_W, SRC_H = 320, 240        # camera capture size (detection + recording)
BAR_X, BAR_W = 320, 80         # status column, right of the preview
PANEL_SCALE = 2

CHAR_W, CHAR_H = 8, 10
TEXT_SCALE = 1

# Changed 2026-10-23c: back to the online page's renderer, user verdict after three font
# rounds -- the DotGothic16 pixel font and the LVGL card layout both read worse than the
# original. The page now draws exactly like titan_lcd_monitor.py: ASCII through the
# firmware ROM font (draw_string), Chinese through 16x16 YaHei bitmaps generated by
#   ui_venv/Scripts/python.exe src/tools/gen_cn_glyphs.py --chars "离线监控状态中等待已锁定全录人数像存储事件启动"
# One row = 16 bits packed into 2 hex bytes, MSB first, 16 rows = 32 bytes per glyph.
# Static labels render once into cached RGB565 patches; volatile ASCII (uptime, fps,
# timers, target tag) goes through draw_string and never allocates a patch, so the
# 10-23 patch-cache leak class stays closed.
CN_GLYPH = 16  # glyph box in pixels
_CN_GLYPHS = {
    "离": "000001007FFE2C20376C33CC3C2C3FFC01003FFC2244264427E42004201C0000",
    "线": "000018D010C830C024FC6FC0784010FE33E03C4C787800321CE27396020C0000",
    "监": "00000440244024FE248025A02730240804001FF812481248124812487FFE0000",
    "控": "00001040102013FC7E061294119813047A0073FC10201020102013FE70000000",
    "状": "000008600868086428602FFE386008600860387068D00898098C0F060E000000",
    "态": "0000018001003FFC018002C006601F38710C01002588248C2436642007E00000",
    "中": "00000180018001807FFE61864186418661867FFE018001800180018001800000",
    "等": "000018601F7E28E0649005901FF801007FFE00203FFC0C300420002001E00000",
    "待": "00000860186033FC606048601FFE1000301853FE110811081098107810700000",
    "已": "00007FF800080008200820083FF83FF82008200020022002200630063FFC0000",
    "锁": "0000102013263D24602061FC3F0613267F261B26132610701CD8198E02020000",
    "定": "0000010001803FFC200420047FFE0080088018F810F818803C8067FC41FC0000",
    "全": "0000018003C006600C301818700E1FF8018001801FF80180018001803FFC0000",
    "录": "00001FF8000800181FF800087FFE0180118C09D803E00FB0719C018607000000",
    "人": "000001800180018001800180018003C00240066004200C301818700E20040000",
    "数": "000000202D200E607F7E0C441EC46D6C08287F28333832300E383BC660820000",
    "像": "0000088009F0123017FC366433FC73C451C416F811B8166C11A4172210E00000",
    "存": "000006000E007FFE0C000BFC180C1010302077FE102010201020106011C00000",
    "储": "00001020146416FC302830783DFE746054FC15C4147C154C1744147C10440000",
    "事": "000001807FFE01801FF811881FF801803FF83FFC018C3FF80188018007000000",
    "件": "000008200920192013FC33FC32207620502017FE102010201020102010200000",
    "启": "0000008000C01FFE100210021FFE1000100037FE34022402240267FE44020000",
    "动": "000000203F20002000FE00227F22182210221662324222427FC6019E011C0000",
}

_cn_label_cache = {}


def cn_label(text, color, bg):
    """Return a cached RGB565 patch with `text` rendered in the 16 px Chinese bitmaps.

    The patch carries `bg` as its own background, so blitting it over a matching fill
    leaves no seam. Any character missing from _CN_GLYPHS renders as blank -- keep the
    label vocabulary inside the generator's --chars set.
    """
    key = (text, color, bg)
    patch = _cn_label_cache.get(key)
    if patch is not None:
        return patch
    width = len(text) * CN_GLYPH
    patch = image.Image(width, CN_GLYPH, image.RGB565)
    patch.draw_rectangle((0, 0, width, CN_GLYPH), color=bg, fill=True)
    for index, char in enumerate(text):
        packed = _CN_GLYPHS.get(char)
        if packed is None:
            continue
        base_x = index * CN_GLYPH
        for row in range(CN_GLYPH):
            bits = int(packed[row * 4:row * 4 + 4], 16)
            if not bits:
                continue
            col = 0
            while bits:
                if bits & 0x8000:
                    patch.set_pixel((base_x + col, row), color)
                bits = (bits << 1) & 0xFFFF
                col += 1
    _cn_label_cache[key] = patch
    return patch


def cn_label_w(text):
    return len(text) * CN_GLYPH


def is_cn_text(text):
    return bool(text) and all(ch in _CN_GLYPHS for ch in text)


def draw_text(x, y, value, color, bg=None, cn_top=None, ascii_top=None):
    # bg must default to None: a PANEL_BG default would be evaluated at def time, and this
    # helper sits above the colour constants in the file.
    if bg is None:
        bg = PANEL_BG
    """Route one string to the right renderer and return its width in px.

    Chinese goes through the cached 16 px bitmap patch (top-left cn_top), ASCII through the
    ROM font (top-left ascii_top); the two fonts have different heights, so callers can
    centre each inside the same slot."""
    if is_cn_text(value):
        canvas.draw_image(cn_label(value, color, bg), x, y if cn_top is None else cn_top)
        return len(value) * CN_GLYPH
    canvas.draw_string((x, y if ascii_top is None else ascii_top), value, color=color)
    return len(value) * CHAR_W


# Changed 2026-10-23c: online-page layout again -- 16 px top/bottom video bars and a plain
# right column (header strip + stacked label/value rows). The LVGL cards, LED pills and
# the accent header are gone together with the pixel font.
HEADER_H = 16
ROW_TOP, ROW_STEP = 24, 42
BAR_PAD = 4                     # inner left padding of the status column

WHITE = (255, 255, 255)
GRAY = (150, 150, 150)
DIM = (122, 132, 148)
AMBER = (255, 184, 76)
GREEN = (96, 220, 128)
RED = (240, 80, 80)
BARS = (16, 18, 24)
PANEL_BG = (10, 12, 16)
HEADER_BG = (26, 34, 46)

# Changed 2026-10-18f: the canvas is created *after* ml.Model (see main), not here.
# Its 187 KB came out of the SRAM pool the NPU arena prefers, and pushing the arena into
# SDRAM tripled model.predict() (43 ms -> 134 ms), which is what made the panel flicker
# on the first connect after power-on.
canvas = None
_bar_cache = {}
_badge_cache = {}
_rec_cache = {}


def fill(x, y, w, h, color):
    canvas.draw_rectangle((x, y, w, h), color=color, fill=True)


def label(x, y, value, color=WHITE, scale=TEXT_SCALE):
    canvas.draw_string((x, y), value, color=color, scale=scale)


def label_right(right, y, value, color=WHITE, scale=TEXT_SCALE):
    """Right-align on the fixed cell grid. mono_space is on, so the width is exact."""
    label(right - len(value) * CHAR_W * scale, y, value, color, scale)


def draw_status_background():
    """Paint the whole canvas once: dark base (the preview blit covers its region every
    frame) plus the header strip on the status column."""
    fill(0, 0, CANVAS_W, CANVAS_H, PANEL_BG)
    fill(BAR_X, 0, BAR_W, HEADER_H, HEADER_BG)
    draw_text(BAR_X + BAR_PAD + 2, 0, TXT["header"], DIM, bg=HEADER_BG, ascii_top=3)


def bar_row(index, name, value, color=WHITE):
    """Draw one status row, skipping the work when nothing changed (online-page bar_row).

    The status column is never touched by the video blit, so its pixels survive between
    presents; redrawing only on change keeps the steady-state cost near zero. The row is
    repainted as a whole so a shorter value cannot leave old glyphs behind.
    """
    key = (name, value, color)
    if _bar_cache.get(index) == key:
        return
    _bar_cache[index] = key
    y = ROW_TOP + index * ROW_STEP
    fill(BAR_X + 1, y - 3, BAR_W - 2, ROW_STEP - 6, PANEL_BG)
    fill(BAR_X + BAR_PAD, y + 1, 2, CN_GLYPH, color)
    draw_text(BAR_X + BAR_PAD + 5, 0, name, DIM, cn_top=y - 2, ascii_top=y + 2)
    draw_text(BAR_X + BAR_PAD + 5, 0, value, color, cn_top=y + 15, ascii_top=y + 15)


def draw_video_bars(uptime, recording, awaiting, capture_fps):
    """Top and bottom strips over the video, online-page geometry. The top-right
    badge carries the guard state: red REC / amber 等待 / green 离线; the bottom
    band keeps uptime left and fps right."""
    fill(0, 0, VIDEO_W, CN_GLYPH, BARS)
    label(5, 3, "TITAN MINI", WHITE)
    if recording:
        label_right(VIDEO_W - 5, 3, "REC", RED)
    else:
        badge = TXT["waiting"] if awaiting else TXT["offline"]
        badge_color = AMBER if awaiting else GREEN
        if is_cn_text(badge):
            canvas.draw_image(cn_label(badge, badge_color, BARS),
                              VIDEO_W - 5 - cn_label_w(badge), 0)
        else:
            label_right(VIDEO_W - 5, 3, badge, badge_color)

    top = VIDEO_H - CN_GLYPH
    fill(0, top, VIDEO_W, CN_GLYPH, BARS)
    label(5, top + 3, uptime, DIM)
    label_right(VIDEO_W - 5, top + 3, "%.1ffps" % capture_fps, DIM)


_rec_block_state = {}


def rec_block(text, color):
    """Big recording timer under the preview -- the one number an operator glances at
    from across the room. The value is ASCII (draw_string, scale 2, allocates nothing);
    the 录像 caption is static. Repaints its block only when the text changes."""
    if _rec_block_state.get(0) == (text, color):
        return
    _rec_block_state[0] = (text, color)
    y0 = VIDEO_H + CN_GLYPH
    fill(0, y0, VIDEO_W, CANVAS_H - y0, PANEL_BG)
    caption = TXT["record"]
    cap_w = len(caption) * (CN_GLYPH if is_cn_text(caption) else CHAR_W)
    draw_text((VIDEO_W - cap_w) // 2, y0 + 24, caption, DIM, bg=PANEL_BG)
    canvas.draw_string(((VIDEO_W - len(text) * CHAR_W * 2) // 2, y0 + 48),
                       text, color=color, scale=2)



def draw_boxes_on_canvas(tracked, frame_scale, subject_id):
    """Live-view boxes on the canvas (the recording boxes are drawn on the frame itself).

    Changed 2026-10-20: the locked target is red and labelled, everybody else is a thin
    green outline. That is the same colour language as the host page, so an operator moving
    between the two never has to relearn what a box means.
    """
    for track in tracked:
        x1, y1, x2, y2, score = track["bbox"]
        x1 = int(x1 * frame_scale)
        y1 = int(y1 * frame_scale)
        x2 = int(x2 * frame_scale)
        y2 = int(y2 * frame_scale)
        is_subject = track["id"] == subject_id
        canvas.draw_rectangle((x1, y1, x2 - x1, y2 - y1),
                              color=RED if is_subject else GREEN,
                              thickness=2 if is_subject else 1)
        # Only the target carries a tag: a crowd of labels costs draw calls on the frame
        # budget and tells the operator nothing he cannot read from the red box.
        if not is_subject:
            continue
        tag = "%d %.2f" % (track["id"], score)
        # Changed 2026-10-23c: back to the online page's ROM-font tag -- the score changes
        # every frame, and draw_string is C-speed and allocates nothing, so the per-frame
        # tag never builds a cached patch (the 10-23 leak class stays closed).
        tag_w = len(tag) * CHAR_W + 6
        tag_x = x1 if x1 + tag_w < VIDEO_W else VIDEO_W - tag_w
        tag_y = y1 - CHAR_H - 3
        if tag_y < HEADER_H:
            tag_y = y1 + 2
        fill(tag_x, tag_y, tag_w, CHAR_H + 2, RED)
        canvas.draw_string((tag_x + 3, tag_y + 1), tag, color=WHITE)


def draw_boxes_on_frame(frame, tracked, subject_id):
    """Burn the boxes into the camera frame so the recording shows them too.

    With a lock only the target is burned (red) -- bystanders are deliberately left out of
    the evidence file. With no lock every person is burned in green. Coordinates are
    already frame pixels: the preview decimation must not shrink the evidence file's
    boxes, so no scaling happens here.
    """
    for track in tracked:
        x1, y1, x2, y2, score = track["bbox"]
        frame.draw_rectangle((x1, y1, x2 - x1, y2 - y1),
                             color=RED if track["id"] == subject_id else GREEN,
                             thickness=2 if track["id"] == subject_id else 1)


def fmt_uptime(ms):
    total = ms // 1000
    return "%02d:%02d:%02d" % (total // 3600, (total // 60) % 60, total % 60)


# --- SD storage helpers --------------------------------------------------------
# Same patterns as the archived standalone tracker: numbered boots, one open/append/close
# per batch of log lines (a FAT append costs 20-100 ms, so lines are buffered).

_log_buffer = []
LOG_BUFFER_LINES = 8
event_count = 0


def _ensure_directories():
    for path in (HOME, LOG_DIR, REC_DIR):
        try:
            os.mkdir(path)
        except OSError:
            pass  # already exists


def _next_boot_number():
    try:
        with open(HOME + "/boot_count.txt") as source:
            number = int(source.read().strip() or "0") + 1
    except OSError:
        number = 1
    with open(HOME + "/boot_count.txt", "w") as sink:
        sink.write(str(number))
    return number


def _next_sequence(directory, prefix, extension):
    highest = 0
    try:
        for name in os.listdir(directory):
            if name.startswith(prefix) and name.endswith(extension):
                stem = name[len(prefix):-len(extension)]
                digits = "".join(ch for ch in stem if ch.isdigit())
                if digits:
                    highest = max(highest, int(digits))
    except OSError:
        pass
    return highest + 1


# Changed 2026-10-18e (SD protection): card capacity and recording budget.
def read_sd_capacity():
    """Resolve the card's total capacity. Returns (total_bytes, source).

    1. os.statvfs('/') -- exact. Needs MICROPY_PY_OS_STATVFS enabled in the firmware
       (currently 0 in platform/include/mpconfigport.h; the RT-Thread port also has to
       use its statfs() syscall -- dfs_posix_titan.c already provides it). Once it works
       the value is cached to sd_capacity_gb.txt so later boots skip straight to it.
    2. sd_capacity_gb.txt on the card -- the override travels with the card, not with
       the code, so a different card only needs its own file.
    3. SD_TOTAL_ASSUMED -- used with a log line, never silently.
    """
    try:
        sv = os.statvfs("/")
        total = sv[1] * sv[2]  # f_frsize * f_blocks
        if total > 0:
            try:
                with open(SD_CAPACITY_FILE, "w") as sink:
                    sink.write(str(total // (1024 * 1024 * 1024)))
            except OSError:
                pass
            return total, "statvfs"
    except (AttributeError, OSError, IndexError):
        pass
    try:
        with open(SD_CAPACITY_FILE) as source:
            gb = float(source.read().strip())
        if gb > 0:
            return int(gb * 1024 * 1024 * 1024), "capacity-file"
    except (OSError, ValueError):
        pass
    return SD_TOTAL_ASSUMED, "assumed-16GB"


# Changed 2026-10-23: a full listdir+stat sweep over /tracker/rec takes ~4.4 s with
# ~170 segments on this card -- and it ran every 30 frames, freezing the LCD for
# seconds at a time while recording (measured: rec_ms 16-25 ms, sweep stall 4.35 s;
# the mjpeg writes were never the stutter). The usage total is cached and maintained
# incrementally instead: +closed segment size, -recycled bytes, one stat of the open
# segment for the display row. Full sweeps now only happen at boot or over-limit.
_rec_used_cache = None


def rec_dir_usage():
    """Total bytes of the stored segments; names come back sorted oldest-first."""
    try:
        names = sorted(os.listdir(REC_DIR))
    except OSError:
        return 0, []
    total = 0
    for name in names:
        try:
            total += os.stat(REC_DIR + "/" + name)[6]
        except OSError:
            continue
    return total, names


def rec_used_cached():
    """Segment bytes from the incremental cache; one sweep on the first call."""
    global _rec_used_cache
    if _rec_used_cache is None:
        _rec_used_cache = rec_dir_usage()[0]
    return _rec_used_cache


def rec_used_add(delta):
    """Book a closed segment's size into the cache (ignored if invalidated)."""
    global _rec_used_cache
    if _rec_used_cache is not None:
        _rec_used_cache += delta


# Changed 2026-10-18b: the panel carries this so the recycle policy is visible at a glance.
def sd_row_value(total_capacity, used):
    """Return (text, color, pct, used_gb) for the 存储 row, tinted by the recycle limit."""
    used_gb = used / 1073741824.0
    pct = (used * 100.0 / total_capacity) if total_capacity else 100.0
    # Changed 2026-10-18c: small cards show one decimal on the total (3.7G), larger ones
    # stay integral (16G); the widest form is 8 cells = 64 px, inside the 69 px budget.
    total_gb = total_capacity / 1073741824.0
    total_text = ("%.1f" if total_gb < 10 else "%.0f") % total_gb
    text = "%.1f/%sG" % (used_gb, total_text)
    if pct >= REC_USAGE_LIMIT * 100:
        color = RED
    elif pct >= 50:
        color = AMBER
    else:
        color = GREEN
    return text, color, pct, used_gb


def enforce_rec_budget(total_capacity):
    """Delete the oldest segments until recordings fit under REC_USAGE_LIMIT of the card.

    Called before a new segment opens (never while one is open). Returns bytes freed.
    Changed 2026-10-23: under the limit this no longer sweeps the directory at all --
    the cached total answers the common case, so opening a segment stays cheap."""
    global _rec_used_cache
    limit = int(total_capacity * REC_USAGE_LIMIT)
    if rec_used_cached() <= limit:
        return 0
    # Over the limit: one real sweep (rare), delete oldest first, refresh the cache.
    used, names = rec_dir_usage()
    freed = 0
    while used > limit and names:
        victim = REC_DIR + "/" + names[0]
        try:
            size = os.stat(victim)[6]
            os.remove(victim)
        except OSError as exc:
            log_event("recycle failed on %s: %s" % (names[0], exc))
            break
        used -= size
        freed += size
        log_event("recycle deleted %s (%.1f MB, recordings over 70%% of card)"
                  % (names[0], size / 1048576.0))
        names.pop(0)
    _rec_used_cache = used
    if freed:
        _flush_log()
        print(ujson.dumps({"kind": "event", "name": "recycle",
                           "payload": {"freed_bytes": freed}}))
    return freed


# Changed 2026-10-24: this firmware's mjpeg writer (lib/imlib/mjpeg.c) never
# emits the idx1 index -- verified on-device with a controlled recording that
# closes cleanly and still ends up without one (probe_fw_mjpeg_close.py), so
# the earlier "only power-loss segments need repair" theory was wrong. Every
# segment the panel records is unplayable until an index is rebuilt. One pass
# here at boot keeps the card playable with zero manual steps: files without
# idx1 get one rebuilt at the logical end (same patch src/tools/
# repair_sd_recordings.py does), legacy .mjpeg and zero-byte files are removed.
def repair_unclosed_segments():
    """Rebuild missing AVI indexes once at boot. Returns (fixed, removed)."""
    try:
        names = sorted(os.listdir(REC_DIR))
    except OSError:
        return 0, 0
    fixed = 0
    removed = 0
    for name in names:
        path = REC_DIR + "/" + name
        try:
            size = os.stat(path)[6]
        except OSError:
            continue
        low = name.lower()
        if low.endswith(".mjpeg") or size == 0:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
            continue
        if not low.endswith(".avi"):
            continue
        try:
            f = open(path, "rb+")
        except OSError:
            continue
        try:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"AVI ":
                f.close()
                continue
            # --- walk top-level chunks: locate movi, check for an index ---
            pos = 12
            movi_data = -1
            movi_size_pos = -1
            has_idx1 = False
            while True:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                fourcc = hdr[:4]
                csz = struct.unpack("<I", hdr[4:8])[0]
                if fourcc == b"idx1":
                    has_idx1 = True
                    break
                if fourcc == b"LIST":
                    ltype = f.read(4)
                    if ltype == b"movi":
                        movi_data = pos + 12
                        movi_size_pos = pos + 4
                    pos += 8 + csz + (csz & 1)
                else:
                    pos += 8 + csz + (csz & 1)
            if has_idx1 or movi_data < 0:
                f.close()
                continue
            # --- scan movi, tolerating a truncated tail from a power loss ---
            entries = []
            off = 0
            while True:
                f.seek(movi_data + off)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cfour = hdr[:4]
                csize = struct.unpack("<I", hdr[4:8])[0]
                if cfour == b"idx1" or cfour == b"JUNK":
                    break
                if movi_data + off + 8 + csize > size:
                    break
                # fourcc stays bytes: pack the three ints and splice (MicroPython
                # struct.pack rejects a bytes argument in an integer field).
                entries.append(cfour + struct.pack("<III", 0x10, off + 4, csize))
                off += 8 + csize + (csize & 1)
            end = movi_data + off
            body = b"".join(entries)
            idx_chunk = b"idx1" + struct.pack("<I", len(body)) + body
            # Write at the LOGICAL end, never the physical one, so the index
            # lands inside the RIFF extent declared below.
            f.seek(end)
            f.write(idx_chunk)
            f.seek(4)
            f.write(struct.pack("<I", end - 8 + len(idx_chunk)))
            f.seek(movi_size_pos)
            f.write(struct.pack("<I", 4 + (end - movi_data)))
            f.close()
            fixed += 1
            log_event("boot repair %s (%d frames)" % (name, len(entries)))
        except OSError:
            try:
                f.close()
            except Exception:
                pass
    if fixed or removed:
        gc.collect()
    return fixed, removed


def _flush_log():
    global _log_buffer
    if not _log_buffer or not log_path:
        _log_buffer = []
        return
    pending = _log_buffer
    _log_buffer = []
    try:
        with open(log_path, "a") as sink:
            for line in pending:
                sink.write(line + "\n")
    except OSError as exc:
        print(ujson.dumps({"kind": "event", "name": "log_write_failed",
                           "payload": {"detail": str(exc)}}))


def log_event(message):
    global event_count
    event_count += 1
    # Changed 2026-10-20: the key/lock helpers can log before the main block sets boot_ms
    # (and the offscreen harness never runs the main block at all), so fall back to 0
    # instead of raising NameError out of a logging call.
    started = globals().get("boot_ms", 0)
    seconds = time.ticks_diff(time.ticks_ms(), started) / 1000.0
    _log_buffer.append("t=%07.1f %s" % (seconds, message))


# --- track-keeper block (host-testable) ---------------------------------------
# Copied from titan_lcd_monitor.py / openmv_ae3_raw_jpeg.py. Keep the three in sync: the
# host suspect keeper assumes the same id cadence from any of these scripts.
IOU_MATCH_THRESHOLD = 0.25
MAX_MISSES = 8
_next_track_id = 1
_active_tracks = []


def _iou(first, second):
    intersection_width = min(first[2], second[2]) - max(first[0], second[0])
    intersection_height = min(first[3], second[3]) - max(first[1], second[1])
    if intersection_width <= 0 or intersection_height <= 0:
        return 0.0
    intersection_width_area = intersection_width * intersection_height
    area_first = (first[2] - first[0]) * (first[3] - first[1])
    area_second = (second[2] - second[0]) * (second[3] - second[1])
    return intersection_width_area / (area_first + area_second - intersection_width_area)


def _remember_motion(track, detection):
    old = track["bbox"]
    side = 0.5 * ((old[2] - old[0]) + (old[3] - old[1]))
    dx = 0.5 * (detection[0] + detection[2] - old[0] - old[2])
    dy = 0.5 * (detection[1] + detection[3] - old[1] - old[3])
    if dx * dx + dy * dy > (1.2 * side) * (1.2 * side):
        track["vx"] = 0.0
        track["vy"] = 0.0
    else:
        track["vx"] = 0.5 * track["vx"] + 0.5 * dx
        track["vy"] = 0.5 * track["vy"] + 0.5 * dy


def _match_score(track, detection):
    x1, y1, x2, y2, _ = track["bbox"]
    vx = track["vx"]
    vy = track["vy"]
    predicted = (x1 + vx, y1 + vy, x2 + vx, y2 + vy)
    overlap = _iou(predicted, detection)
    centre_x = 0.5 * (predicted[0] + predicted[2])
    centre_y = 0.5 * (predicted[1] + predicted[3])
    dx = 0.5 * (detection[0] + detection[2]) - centre_x
    dy = 0.5 * (detection[1] + detection[3]) - centre_y
    side = 0.5 * ((predicted[2] - predicted[0]) + (predicted[3] - predicted[1]))
    distance_squared = dx * dx + dy * dy
    reach = 0.9 * side
    if distance_squared > reach * reach:
        return overlap
    fallback = IOU_MATCH_THRESHOLD + 0.25 * (1.0 - (distance_squared ** 0.5) / reach)
    return overlap if overlap > fallback else fallback


def _update_tracks(detections):
    global _next_track_id, _active_tracks
    for track in _active_tracks:
        track["misses"] += 1
    unmatched = list(range(len(detections)))
    for track in _active_tracks:
        best_index = -1
        best_score = IOU_MATCH_THRESHOLD
        for index in unmatched:
            score = _match_score(track, detections[index])
            if score > best_score:
                best_score = score
                best_index = index
        if best_index >= 0:
            _remember_motion(track, detections[best_index])
            track["bbox"] = detections[best_index]
            track["misses"] = 0
            unmatched.remove(best_index)
    for index in unmatched:
        _active_tracks.append({
            "id": _next_track_id,
            "bbox": detections[index],
            "vx": 0.0,
            "vy": 0.0,
            "misses": 0,
        })
        _next_track_id += 1
    _active_tracks = [track for track in _active_tracks if track["misses"] <= MAX_MISSES]
    return _active_tracks
# --- end track-keeper block ----------------------------------------------------


def detect(model, frame):
    detections = []
    results = model.predict([frame])
    if results:
        for entry in results[PERSON_CLASS]:
            x, y, width, height = entry[0]
            x2 = min(frame.width() - 1, int(x + width))
            y2 = min(frame.height() - 1, int(y + height))
            x1 = max(0, int(x))
            y1 = max(0, int(y))
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append((x1, y1, x2, y2, float(entry[1])))
    return detections


def boot_screen(message):
    fill(0, 0, CANVAS_W, CANVAS_H, PANEL_BG)
    label((CANVAS_W - 10 * CHAR_W * 2) // 2, 92, "TITAN MINI", WHITE, 2)
    fill(CANVAS_W // 2 - 40, 122, 80, 2, GREEN)
    # Changed 2026-10-23c: draw_text routes Chinese to the cached patch, ASCII to the ROM
    # font -- same two-renderer rule as the rest of the page.
    msg_w = len(message) * (CN_GLYPH if is_cn_text(message) else CHAR_W)
    draw_text((CANVAS_W - msg_w) // 2, 132, message, DIM, ascii_top=134)
    label((CANVAS_W - len(BUILD_TAG) * CHAR_W) // 2, 150, BUILD_TAG, GRAY)
    lcd.display(canvas, x_scale=PANEL_SCALE, y_scale=PANEL_SCALE)


def force_clean_slate():
    """Changed 2026-10-18d: whatever the previous page left open gets closed here.

    The host uploads this app right after stopping the online page, and that stop can arrive
    while the page is still tearing down. Closing the panel and the CSI pipeline up front
    means the guard always owns the hardware instead of inheriting it.
    """
    try:
        lcd.deinit()
    except BaseException:
        pass
    try:
        csi.CSI().shutdown(True)
    except BaseException:
        pass


def start_camera():
    """Changed 2026-10-18d: retry instead of dying, and let AE/AWB settle in steps."""
    last_error = None
    for attempt in range(3):
        try:
            cam = csi.CSI()
            cam.reset()
            cam.pixformat(csi.RGB565)
            cam.framesize(csi.QVGA)
            for _ in range(3):
                cam.snapshot(time=800)
            return cam
        except BaseException as exc:  # noqa: BLE001 - a failed init must not kill the guard
            last_error = exc
            _flush_log()
            time.sleep_ms(400)
    raise last_error


camera = None
try:
    force_clean_slate()
    lcd.init()
    lcd.backlight(BACKLIGHT)
    # Changed 2026-10-18d: model before canvas. The NPU arena wants one contiguous block out of
    # the 1.8 MB SRAM pool and drops to SDRAM when it cannot get one, which triples predict()
    # time (43 ms -> 134 ms) and breaks the frame budget -- the panel then flickers.
    lcd.clear()  # black while the model loads; the boot screen follows immediately
    model = ml.Model(MODEL_PATH, postprocess=YoloV8(threshold=SCORE_THRESHOLD))
    canvas = image.Image(CANVAS_W, CANVAS_H, image.RGB565)
    _bar_cache.clear()  # a fresh canvas has no cached rows on it
    boot_screen(TXT["boot"])

    boot_ms = time.ticks_ms()
    _ensure_directories()
    boot_number = _next_boot_number()
    log_path = "%s/guard_boot%03d.log" % (LOG_DIR, boot_number)
    # Changed 2026-10-20: segments are written with an .avi extension. The recorder has
    # always produced a real AVI container (RIFF/AVI, MJPG fourcc -- lib/imlib/mjpeg.c),
    # so .mjpeg only made the files un-openable by double-click. Same files, honest name.
    rec_sequence = _next_sequence(REC_DIR, "boot%03d_" % boot_number, ".avi")
    log_event("boot build=%s canvas=%dx%d" % (BUILD_TAG, CANVAS_W, CANVAS_H))
    # Changed 2026-10-18e (SD protection): resolve the card capacity once and enforce the
    # 70% budget immediately, so an over-full card is trimmed before the first segment.
    sd_total, sd_source = read_sd_capacity()
    log_event("sd capacity %d MB (source: %s)" % (sd_total // 1048576, sd_source))
    # Changed 2026-10-24: heal the card before the budget sweep so the cached
    # total includes the freshly written indexes.
    rec_fixed, rec_removed = repair_unclosed_segments()
    if rec_fixed or rec_removed:
        log_event("boot repair: fixed %d segments, removed %d junk files"
                  % (rec_fixed, rec_removed))
    enforce_rec_budget(sd_total)
    _flush_log()

    draw_status_background()

    camera = start_camera()

    # Deliberately NOT board.preview(False): that would switch off the framebuffer the host
    # stream channel reads from and break the next host session (see the online page).
    print(ujson.dumps({
        "kind": "event",
        "name": "device_boot",
        "payload": {"page": "guard", "build": BUILD_TAG, "canvas": [CANVAS_W, CANVAS_H],
                    "boot": boot_number},
    }))

    # Changed 2026-10-19: after device_boot, so a slow touch reset never delays the
    # heartbeat the host watchdog is waiting for. Same for the key: it only needs to be
    # ready by the first frame the operator could see.
    touch_init()
    key_init()

    tracked = []
    frame_index = 0
    infer_ms = 0.0
    rec_ms = 0.0
    capture_fps = 0.0
    fps_frames = 0
    fps_start = boot_ms

    recorder = None
    rec_path = None
    rec_start_ticks = 0
    rec_empty_ticks = 0
    rec_seconds = 0.0
    rec_disabled = False
    # Changed 2026-10-20: frame index on which the current segment was opened. REC_EVERY
    # and DETECT_EVERY are both 2, so the opening frame would otherwise also take a write
    # in the same pass (open 41 ms + write 21 ms = 62 ms of a 75 ms budget).
    rec_open_index = -1
    # Changed 2026-10-18e: after a suspect leaves the frame the guard stops the segment and
    # waits for a confirmation before arming the next one.
    # Changed 2026-10-20: that confirmation no longer comes from the touch panel (which does
    # not answer) but from a 1.5 s timer, with a tap or a key press as optional shortcuts.
    # The old 30 s touch wait was a recording blind spot: anyone walking in during it was
    # simply not recorded.
    awaiting_touch = False
    rearm_at = 0
    # Changed 2026-10-18b: initial SD row (the first call here fills the usage cache
    # with a single sweep; the loop below only does one stat per refresh from now on).
    sd_text, sd_color, sd_pct, sd_used_gb = sd_row_value(sd_total, rec_used_cached())

    while True:
        frame_start = time.ticks_ms()

        present_start = time.ticks_us()
        lcd.display(canvas, x_scale=PANEL_SCALE, y_scale=PANEL_SCALE)
        present_ms = time.ticks_diff(time.ticks_us(), present_start) / 1000.0

        snapshot_start = time.ticks_us()
        frame = camera.snapshot()
        if frame is None:
            time.sleep_ms(5)
            continue

        frame_index += 1
        if frame_index % DETECT_EVERY == 0:
            infer_start = time.ticks_us()
            tracked = _update_tracks(detect(model, frame))
            infer_ms = time.ticks_diff(time.ticks_us(), infer_start) / 1000.0

        frame_scale = VIDEO_W / frame.width()
        if frame_scale == 1.0:
            canvas.draw_image(frame, 0, 0)
        else:
            canvas.draw_image(frame, 0, 0, x_scale=frame_scale, y_scale=frame_scale)

        fps_frames += 1
        window = time.ticks_diff(frame_start, fps_start)
        if window >= 1000:
            capture_fps = fps_frames * 1000.0 / window
            fps_frames = 0
            fps_start = frame_start

        persons = 0
        for track in tracked:
            if track["misses"] == 0:
                persons += 1

        # --- operator input ------------------------------------------------------
        # Changed 2026-10-20: USER_KEY replaces the touch panel. Single = arm (and restore
        # auto-lock after a long press), double = cycle the target, long = record everybody.
        now = time.ticks_ms()
        action = poll_key(now)
        if action:
            apply_key(action, tracked, now)

        # --- target lock ---------------------------------------------------------
        subject = lock_step(tracked, now, rec_path is None)
        subject_id = subject["id"] if subject is not None else -1
        # With a target the segment follows that person; with no lock (long press) any
        # person keeps the recorder armed, which is the old behaviour.
        target_present = (subject is not None) if _lock_id >= 0 else (persons > 0)

        draw_boxes_on_canvas(tracked, frame_scale, subject_id)
        draw_video_bars(fmt_uptime(time.ticks_diff(now, boot_ms)),
                        rec_path is not None, awaiting_touch, capture_fps)

        # --- recording lifecycle -------------------------------------------------
        # Arm/disarm. Changed 2026-10-20: the touch confirmation is gone; a closed segment
        # re-arms itself after REARM_DELAY_MS, and a tap or key press only makes it faster.
        if awaiting_touch and not rec_path:
            if read_touch_confirmed():
                awaiting_touch = False
                log_event("touch confirmed; guard re-armed")
                _flush_log()
            elif time.ticks_diff(now, rearm_at) >= REARM_DELAY_MS:
                awaiting_touch = False
                log_event("auto re-arm (%.1fs after the segment closed)" % (REARM_DELAY_MS / 1000.0))
                _flush_log()
        if not rec_disabled and not awaiting_touch:
            # Changed 2026-10-20: the segment is opened on a non-inference frame only.
            # Opening costs 41-58 ms (measured) and inference 39 ms; colliding them in one
            # 75 ms frame is exactly the one-frame hitch seen when a person appears.
            if rec_path is None and target_present and frame_index % DETECT_EVERY == 1:
                # SD protection first: recycle the oldest segments if recordings are
                # over 70% of the card before this new one starts.
                enforce_rec_budget(sd_total)
                rec_path = "%s/boot%03d_%02d.avi" % (REC_DIR, boot_number, rec_sequence)
                recorder = None
                try:
                    recorder = mjpeg.Mjpeg(rec_path)
                except OSError as exc:
                    log_event("rec open failed: %s" % exc)
                    _flush_log()
                    rec_path = None
                    rec_disabled = True  # do not retry every frame on a broken card
                if recorder is not None:
                    rec_start_ticks = time.ticks_ms()
                    rec_empty_ticks = 0
                    rec_open_index = frame_index
                    log_event("rec_start %s (persons=%d target=%d)" % (rec_path, persons, _lock_id))
                    _flush_log()
                    print(ujson.dumps({"kind": "event", "name": "rec_start",
                                       "payload": {"path": rec_path, "target": _lock_id}}))
            elif rec_path is not None:
                if not target_present:
                    if rec_empty_ticks == 0:
                        rec_empty_ticks = now
                    elif time.ticks_diff(now, rec_empty_ticks) >= REC_STOP_AFTER_EMPTY_S * 1000:
                        recorder.close()
                        # Changed 2026-10-23: book the closed size into the usage cache.
                        try:
                            rec_used_add(os.stat(rec_path)[6])
                        except OSError:
                            pass
                        log_event("rec_stop %s (target left frame)" % rec_path)
                        _flush_log()
                        print(ujson.dumps({"kind": "event", "name": "rec_stop",
                                           "payload": {"path": rec_path}}))
                        recorder = None
                        rec_path = None
                        rec_empty_ticks = 0
                        rec_sequence += 1
                        # Changed 2026-10-20: a short self re-arm instead of a 30 s wait
                        # for a touch that never arrives.
                        awaiting_touch = True
                        rearm_at = now
                else:
                    rec_empty_ticks = 0
                if rec_path is not None:
                    rec_seconds = time.ticks_diff(now, rec_start_ticks) / 1000.0
                    if rec_seconds >= REC_MAX_SECONDS:
                        recorder.close()
                        # Changed 2026-10-23: book the closed size into the usage cache.
                        try:
                            rec_used_add(os.stat(rec_path)[6])
                        except OSError:
                            pass
                        log_event("rec_stop %s (max duration)" % rec_path)
                        _flush_log()
                        recorder = None
                        rec_path = None
                        rec_sequence += 1

        # Write the annotated camera frame on non-inference frames only: the mjpeg write
        # costs ~35 ms (spikes ~190 ms on cluster flushes) and inference frames are the
        # ones closest to the budget.
        # Changed 2026-10-20: skip the frame that just opened the segment -- the open call
        # already cost 41-58 ms, adding a write to the same frame is the visible hitch.
        if (recorder is not None and frame_index % REC_EVERY == 1
                and frame_index != rec_open_index):
            draw_boxes_on_frame(frame, tracked, subject_id)
            write_start = time.ticks_us()
            try:
                recorder.write(frame, quality=REC_QUALITY)
                rec_ms = time.ticks_diff(time.ticks_us(), write_start) / 1000.0
            except OSError as exc:
                recorder.close()
                # Changed 2026-10-23: book the closed size into the usage cache.
                try:
                    rec_used_add(os.stat(rec_path)[6])
                except OSError:
                    pass
                log_event("rec write failed: %s" % exc)
                _flush_log()
                recorder = None
                rec_path = None
                rec_disabled = True

        # Changed 2026-10-23c: plain rows again (online-page bar_row) instead of LVGL
        # cards. Capture fps lives in the footer, infer in telemetry only.
        if awaiting_touch:
            state_text, state_color = TXT["waiting"], AMBER
        elif not _auto_lock:
            state_text, state_color = TXT["allrec"], DIM
        elif _lock_id >= 0:
            state_text, state_color = TXT["locking"], GREEN
        else:
            state_text, state_color = TXT["guarding"], GREEN
        bar_row(0, TXT["state"], state_text, state_color)
        bar_row(1, TXT["persons"], "%d/%d" % (persons, _next_track_id - 1), WHITE)
        if rec_path is not None:
            # Segments cap at 5 min, so minutes stay single-digit ("5:00" = 4 cells).
            bar_row(2, TXT["record"], "%d:%02d" % (rec_seconds // 60, rec_seconds % 60), RED)
        elif awaiting_touch:
            bar_row(2, TXT["record"], "WAIT", AMBER)
        else:
            bar_row(2, TXT["record"], "STBY", GRAY)
        # Changed 2026-10-18b: SD footprint refreshed once per second (a listdir + stat
        # sweep over the segments is not free, so it does not run every frame).
        if frame_index % 30 == 0:
            # Changed 2026-10-23: one stat of the open segment instead of the full
            # directory sweep -- the sweep was the 4.4 s LCD stall during recording.
            used_now = rec_used_cached()
            if rec_path is not None:
                try:
                    used_now += os.stat(rec_path)[6]
                except OSError:
                    pass
            sd_text, sd_color, sd_pct, sd_used_gb = sd_row_value(sd_total, used_now)
        bar_row(3, TXT["sd"], sd_text, sd_color)
        bar_row(4, TXT["events"], "%d" % event_count, DIM)

        if frame_index % 240 == 0:
            gc.collect()

        if frame_index % 30 == 0:
            _flush_log()
            print(ujson.dumps({
                "kind": "telemetry",
                "name": "frame_cost",
                "payload": {
                    "present_ms": round(present_ms, 1),
                    "infer_ms": round(infer_ms, 1),
                    # Changed 2026-10-20: the write cost is what the stutter complaint was
                    # about; keeping it in telemetry makes the next tuning data-driven.
                    "rec_ms": round(rec_ms, 1),
                    "fps": round(capture_fps, 1),
                    "state": "rec" if rec_path is not None else ("wait" if awaiting_touch else "idle"),
                    "events": event_count,
                    "free_kb": gc.mem_free() // 1024,
                    # Changed 2026-10-20: lets the host see what the offline guard decided
                    # on its own (target id and whether the lock is auto or cleared).
                    "target": _lock_id,
                    "auto_lock": 1 if _auto_lock else 0,
                    # Changed 2026-10-18b: lets the host verify the 70% policy remotely.
                    "sd_used_gb": round(sd_used_gb, 3),
                    "sd_pct": round(sd_pct, 2),
                },
            }))

        # Changed 2026-10-20: the idle part of the frame is spent in 5 ms slices that keep
        # polling USER_KEY, so a press is noticed within ~5 ms instead of waiting for the
        # next 75 ms frame (or, worse, for a 33 ms SD write to finish).
        idle_ms = FRAME_PERIOD_MS - time.ticks_diff(time.ticks_ms(), frame_start)
        while idle_ms > 0:
            slice_ms = 5 if idle_ms > 5 else idle_ms
            time.sleep_ms(slice_ms)
            idle_ms -= slice_ms
            late = poll_key(time.ticks_ms())
            if late:
                apply_key(late, tracked, time.ticks_ms())
finally:
    # Changed 2026-10-18g: every step gets its own guard, camera and panel first. The host's
    # EXEC ioctl schedules a second KeyboardInterrupt on top of the STOP one, so an interrupt
    # can land in the middle of this block (measured: it aborted right after the 1.5 s SD
    # flush). Whichever step is running still gets aborted, but the remaining ones now run --
    # previously the camera stayed on and the panel stayed open, which is exactly what made
    # the next page flicker until the port was re-opened.
    try:
        if camera is not None:
            camera.shutdown(True)
    except BaseException:
        pass
    try:
        lcd.deinit()
    except BaseException:
        pass
    try:
        if recorder is not None:
            recorder.close()
            log_event("rec_stop %s (shutdown)" % rec_path)
            _flush_log()
    except BaseException:
        pass
    # Changed 2026-10-18d: release the model, its NPU workspace and the canvas. This script is
    # what /main.py runs at power-on, and a boot-time script keeps its `__main__` globals alive
    # after the host's KeyboardInterrupt, so gc could never reclaim them: measured 3.2 MB still
    # allocated after the hand-over, which made the online page's predict() take 132 ms instead
    # of 43 ms and the panel flicker until the next connect.
    try:
        model = None
        canvas = None
        # Changed 2026-10-23: hand the whole heap back, patches included. The label
        # patches (hundreds of KB after a long session) kept the __main__ globals alive
        # and fragmented SRAM for the online page's NPU arena.
        _bar_cache.clear()
        _badge_cache.clear()
        _rec_block_state.clear()
        _cn_label_cache.clear()
        gc.collect()
    except BaseException:
        pass
