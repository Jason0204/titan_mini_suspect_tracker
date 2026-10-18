# OpenMV AE3 CDC 协议学习笔记

本文记录 OpenMV AE3 通过 USB CDC 与 PC 通信、运行 MicroPython 脚本和读取视频帧的协议流程，供后续排查、扩展和重新实现时参考。

## 1. 两套协议不可混用

本工程有两种不同的视频协议：

| 协议 | 用途 | 线格式 |
| --- | --- | --- |
| OpenMV CDC Protocol | OpenMV AE3 官方固件与 OpenMV IDE/上位机通信 | 二进制命令包，脚本和帧通过 `stdin`、`stream` 通道传输 |
| Titan `TVF1` | 项目通用硬件视频接口 | 32 字节自定义头加图像 payload |

AE3 的 USB VID/PID 是 `37C5:16E3`，Windows 会将 runtime USB CDC 枚举成 COM 口。它不是通用 UVC 摄像头，也不会等待本项目的 `TVF1` 头。打开 COM 只代表设备句柄打开，仍需完成 OpenMV 协议同步。

当前工程保留 `TVF1` 解码路径供 Titan 通用硬件使用；AE3 使用 OpenMV CDC 协议，数据最终共用 `frame_ready(frame, detections)` 和 UI 显示链路。

## 2. 数据包结构

所有多字节整数均为 little-endian。固定头长 10 字节；payload 非空时，在 payload 后另附 4 字节 CRC32。

| Offset | 长度 | 字段 | 说明 |
| ---: | ---: | --- | --- |
| 0 | 2 | `sync` | `0xD5AA`，线上字节为 `AA D5` |
| 2 | 1 | `sequence` | 8 位命令序号，按 0 到 255 回绕 |
| 3 | 1 | `channel` | 通道编号；系统命令使用 0 |
| 4 | 1 | `flags` | ACK、NAK、重传、分片、事件等标志 |
| 5 | 1 | `opcode` | 命令编号 |
| 6 | 2 | `length` | payload 字节数 |
| 8 | 2 | `header_crc16` | 覆盖头前 8 字节 |
| 10 | `length` | `payload` | 可选命令参数或返回数据 |
| 末尾 | 4 | `payload_crc32` | payload 非空时存在，覆盖 payload |

工程中的结构定义：

```python
HEADER_WITHOUT_CRC = struct.Struct("<HBBBBH")
HEADER = struct.Struct("<HBBBBHH")
```

CRC 参数：

- CRC16 多项式 `0xF94F`，初值 `0xFFFF`，覆盖头前 8 字节。
- CRC32 多项式 `0xFA567D89`，初值 `0xFFFFFFFF`，覆盖 payload。
- OpenMV CRC32 不是常见 ZIP/Ethernet CRC32，不能直接用 `zlib.crc32` 代替。
- 空 payload 包不附 CRC32。

USB `read()` 的边界不等于协议包边界：一次 read 可能只拿到半个包，也可能拿到多个包。接收端必须按 sync、头部长度和 payload length 增量组包，并校验 CRC。

## 3. 标志位与响应规则

| Bit | 名称 | 含义 |
| ---: | --- | --- |
| 0 | `ACK` | 成功确认 |
| 1 | `NAK` | 命令失败；payload 前 2 字节为状态码 |
| 2 | `RTX` | 重传包 |
| 3 | `ACK_REQ` | 接收端应回复 ACK |
| 4 | `FRAGMENT` | 当前 payload 是多片响应的一部分 |
| 5 | `EVENT` | 异步事件，不是当前命令的正常响应 |

处理约定：

1. 对 `ACK_REQ` 包回复空 ACK，序号、通道和 opcode 与收到的包一致。
2. 事件包单独处理，不消费普通命令的序号。
3. 普通响应序号应与当前期望值一致；消费后序号加一并按 8 位回绕。
4. 分片响应要累积 payload，直到最后一片不再带 `FRAGMENT`。
5. 成功但无 payload 的 ACK 在高层 API 中可表示为 `True`，不是超时或无响应。
6. NAK 状态码：`0 SUCCESS`、`1 FAILED`、`2 INVALID`、`3 TIMEOUT`、`4 BUSY`、`5 CHECKSUM`、`6 SEQUENCE`、`7 OVERFLOW`、`8 FRAGMENT`、`9 UNKNOWN`。`BUSY` 可稍候重试，其他状态应显示出来供诊断。

## 4. 连接和能力协商

OpenMV 官方 host 客户端的基本流程：

1. 打开 AE3 USB CDC COM 口，默认 baud 参数 `921600`。这是 USB CDC 驱动兼容参数，不代表链路是物理 UART。
2. 初始 sequence 设为 0，初始最大 payload 设为 52 字节。
3. 发送 `PROTO_SYNC (0x00)`。OpenMV host 实现会在同步失败时重试；同步成功后 sequence 重置为 0。
4. 发送 `PROTO_GET_CAPS (0x01)`，响应前 6 字节按 `<IH` 解析为 capabilities flags 和设备最大 payload。
5. 发送 `PROTO_SET_CAPS (0x02)`，payload 按 `<IH10x` 打包：请求功能 flags、协商 payload 上限和 10 字节保留区。当前 host 请求 CRC、sequence、ACK、events，即低 4 位为 1。
6. 发送 `CHANNEL_LIST (0x20)` 获取通道列表。

通道列表每项固定 16 字节，按 `<BB14s` 解析：`channel_id`、通道 flags、14 字节 NUL 结尾名称。AE3 视频工作流至少要求存在：

- `stdin`：停止、上传并运行设备端 MicroPython 脚本。
- `stream`：开启/关闭帧流并读取图像。
- 固件还可能提供 `stdout`、`profile` 等其他通道。

如果同步阶段超时，检查 runtime VCP 是否选对、COM 是否被 OpenMV IDE 占用，以及是否使用了更新后的程序。`hardware.log` 应记录同步重试、opcode、channel、sequence、已收到的数据量和缓冲区尾部。

## 5. 常用命令

### Opcode

| 命令 | 值 | 用途 |
| --- | ---: | --- |
| `PROTO_SYNC` | `0x00` | 同步协议 |
| `PROTO_GET_CAPS` | `0x01` | 读取协议能力 |
| `PROTO_SET_CAPS` | `0x02` | 协商协议能力 |
| `PROTO_STATS` | `0x03` | 读协议统计 |
| `PROTO_VERSION` | `0x04` | 读固件/协议版本 |
| `SYS_RESET` | `0x10` | 软复位 |
| `SYS_BOOT` | `0x11` | 进入 bootloader |
| `SYS_INFO` | `0x12` | 读芯片和硬件能力 |
| `CHANNEL_LIST` | `0x20` | 列出通道 |
| `CHANNEL_POLL` | `0x21` | 轮询通道数据状态 |
| `CHANNEL_LOCK` | `0x22` | 锁定通道缓冲区 |
| `CHANNEL_UNLOCK` | `0x23` | 解锁通道缓冲区 |
| `CHANNEL_SIZE` | `0x25` | 查询可读字节数 |
| `CHANNEL_READ` | `0x26` | 从通道读数据 |
| `CHANNEL_WRITE` | `0x27` | 向通道写数据 |
| `CHANNEL_IOCTL` | `0x28` | 执行通道控制操作 |

### 通道命令 payload

- `CHANNEL_SIZE` 返回一个 little-endian `uint32`，表示可读字节数。
- `CHANNEL_READ` 请求 payload 是 `<II`：`offset, length`。
- `CHANNEL_WRITE` 请求 payload 是 `<II` 加数据：`offset, length, bytes`。单包上限是协商的最大 payload；脚本大于上限时分多个 write 命令发送。
- `CHANNEL_IOCTL` 的 payload 是 `<I` ioctl 编号，后面可追加参数。

`stdin` ioctl：

| ioctl | 值 | 用途 |
| --- | ---: | --- |
| `STDIN_STOP` | `1` | 中断相机上现有脚本 |
| `STDIN_EXEC` | `2` | 执行已写入 stdin 缓冲区的脚本 |
| `STDIN_RESET` | `3` | 清空脚本缓冲区 |

`stream` ioctl：

| ioctl | 值 | 参数 |
| --- | ---: | --- |
| `STREAM_CTRL` | `0` | `<I`，0 关闭、1 开启 |
| `STREAM_RAW_CTRL` | `1` | `<I`，0 表示非 raw 格式 |
| `STREAM_RAW_CFG` | `2` | raw 模式下传 `<II` 宽、高 |

## 6. 上传并执行设备脚本

AE3 使用 OpenMV CDC 的 stdin 通道，不是 `Ctrl-A` Raw REPL。最小流程：

```text
CHANNEL_IOCTL(stdin, STDIN_STOP)
等待约 500 ms
CHANNEL_IOCTL(stdin, STDIN_RESET)
CHANNEL_WRITE(stdin, offset + length + script bytes)，按 max_payload 分块
CHANNEL_IOCTL(stdin, STDIN_EXEC)
```

先停止旧脚本很重要：旧程序可能仍占用 stdin/stdout。当前示例 [openmv_ae3_raw_jpeg.py](../src/device/openmv_ae3_raw_jpeg.py) 文件名沿用早期原型，但内容是 AE3 CSI 采集脚本：创建 `csi.CSI()`、设置 RGB565/QVGA、循环 `snapshot()` 并 `flush()`。图像发送由 OpenMV 固件的 `stream` 通道完成，脚本不手工发送 USB bytes，也不添加 `TVF1` 头。

**注意：脚本开始运行会把设备侧协议序号计数清零，而 host 侧计数器仍在继续累加。** 如果 host 在 `STDIN_EXEC` 之后立刻轮询 `CHANNEL_LIST`，就可能出现 `OpenMV sequence mismatch: expected N, got 0` 这类报错（`N` 等于此刻 host 的计数器值）。这不是链路故障，而是两侧计数器失步。正确做法是：`STDIN_EXEC` 之后稍等片刻再轮询，并且在收到序号不匹配的响应时执行一次 `PROTO_SYNC` 重新对时（`PROTO_SYNC` 会把两侧计数器同时复位到 0），然后重放该命令，而不是直接断开连接。

## 7. 视频帧布局

Host 开启 stream 后读取一帧的顺序：

```text
CHANNEL_LOCK(stream)
CHANNEL_SIZE(stream) -> size
CHANNEL_READ(stream, offset=0, length=size)
CHANNEL_UNLOCK(stream)
```

通道帧开头 20 字节按 `<IIIII` 解析：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `width` | `uint32` | 宽度 |
| `height` | `uint32` | 高度 |
| `pixformat` | `uint32` | OpenMV 像素格式常量 |
| `depth` | `uint32` | 位深 |
| `offset` | `uint32` | 图像数据在整个通道 payload 中的起始偏移 |

图像数据从 `data[offset:]` 开始。不要固定假设 offset 等于 20；固件可能加入 padding 或其他字段。当前实现识别的像素格式：

| 格式 | 常量值 | 解码方式 |
| --- | ---: | --- |
| 灰阶 | `0x08020001` | 每像素 1 字节，转 BGR 三通道 |
| RGB565 | `0x0C030002` | 每像素 2 字节，拆分 R5/G6/B5 并扩展到 8 位 |
| JPEG | `0x06060000` | OpenCV JPEG 解码 |

项目 UI、跟踪框和录像仍统一使用 `uint8 H x W x 3`、BGR、C-contiguous ndarray。RGB565 扩位要用足够宽的中间整数，避免 `uint8 * 255` 溢出。

stream 读取使用 lock/unlock；无帧、解码失败或异常都必须释放锁。由于 frame reader 和 UI 控制运行在不同线程，整段 `lock -> size -> read -> unlock` 需要作为一个串行事务保护，不能和 `streaming(False)` 等命令交错。

## 8. 本工程代码入口

| 职责 | 文件 |
| --- | --- |
| OpenMV CDC 包、CRC、同步/协商、通道命令、脚本执行、读帧 | [src/suspect_tracker/openmv_protocol.py](../src/suspect_tracker/openmv_protocol.py) |
| 打开 COM、运行 AE3 脚本、像素转换、发 Qt `frame_ready` | [src/suspect_tracker/hardware_video.py](../src/suspect_tracker/hardware_video.py) |
| UI 连接、暂停/恢复和视频显示 | [src/suspect_tracker/ui/main_window.py](../src/suspect_tracker/ui/main_window.py) |
| AE3 CSI 采集脚本 | [src/device/openmv_ae3_raw_jpeg.py](../src/device/openmv_ae3_raw_jpeg.py) |
| Titan 通用 `TVF1` 帧协议 | [docs/hardware_video_protocol.md](hardware_video_protocol.md) |

OpenMV 官方 host SDK 的较新版本要求高于当前项目的 Python 3.11 环境，因此工程实现的是 AE3 视频所需的最小 OpenMV CDC 协议子集，并非完整 SDK。若要增加版本信息、系统状态、stdout、自定义通道、事件或 profiler，应继续按官方协议扩展，或评估升级 Python 后复用官方 `openmv.Camera`。

## 9. 调试清单

> 日志位置（2026-10-14 起）：`<程序目录>\log\hardware_YYYY-MM-DD.log`（协议 trace）、
> `<程序目录>\log\ui_events_YYYY-MM-DD.log`（界面日志）、
> `<程序目录>\log\startup_YYYY-MM-DD.log`（启动阶段）。打包运行时程序目录就是 EXE 所在目录
> （例如 `dist\`）；源码运行时是工程根。详见 §12.5。

1. **打不开 COM**：确认设备管理器中的设备是 AE3 runtime VCP（`37C5:16E3`），关闭 OpenMV IDE，确认端口没有被占用。
2. **`PROTO_SYNC` 超时**：确认使用新版程序；查看 `hardware.log` 中 `PROTO_SYNC attempt`、待读字节数和缓冲区尾部。新版会重试同步 3 次。
3. **通道协商失败**：检查 `PROTO_GET_CAPS`、`PROTO_SET_CAPS` 和 `CHANNEL_LIST` 的 opcode、sequence、CRC、响应长度；确认固件注册了 `stdin` 和 `stream`。
4. **脚本没运行**：检查 `STDIN_STOP -> STDIN_RESET -> CHANNEL_WRITE -> STDIN_EXEC` 每一步的 ACK/NAK 和状态码；检查固件是否提供 `csi.CSI()`。
5. **协议已连但没有图像**：新版有看门狗自愈，`hardware.log` 会记录全过程：
   - `OpenMV streaming enabled/disabled`：每次启停流都有，可确认流状态。
   - `First OpenMV protocol frame decoded`：第一帧解码成功。
   - `OpenMV frame heartbeat: N frames decoded`：每 150 帧一条，证明链路存活。
   - `OpenMV watchdog: no new frames for 3s; re-asserting stream control`：3 秒无帧 → 自动重发 `STREAM_CTRL=1` 并重启取帧线程。
   - `OpenMV watchdog: still no frames; re-uploading the AE3 capture script`：6 秒仍无帧 → 自动重传采集脚本。
   - `OpenMV watchdog gave up`：9 秒仍无帧才报错。
   实测：人为关闭设备端推流后，看门狗在 6 秒内自动恢复画面（帧计数继续增长）。
6. **颜色不对**：检查 RGB565 的字节序、RGB/BGR 通道顺序和测试色块结果。
7. **`sequence mismatch: expected N, got 0`**：设备脚本重启导致序号计数器归零，属于可恢复失步。查看 `hardware.log` 里 `OpenMV resync requested` / `OpenMV resync complete` 是否紧跟其后；若重同步后仍反复出现同一偏移量，才说明设备侧没有真正响应 `PROTO_SYNC`。
8. **连接成功、协议正常，但界面黑屏**：优先查 `hardware.log` 里的
   `OpenMV stream reader failed: OpenMV channel is not registered: tracker`。
   出现这句说明**host 侧取帧线程已经退出**——它在轮询本固件不存在的 `tracker` 通道时抛异常并被终止，
   与设备、线缆、`STREAM_CTRL` 都无关。新版取帧线程只在通道确实存在时才轮询事件，
   单帧解码失败也只在连续 10 次后才放弃。若仍黑屏，看是否有
   `OpenMV frame read failed (n/10)` 或 `First OpenMV protocol frame decoded` 诊断行。

协议参考：OpenMV 官方 [openmv-python](https://github.com/openmv/openmv-python) 中的 `camera.py`、`transport.py`、`constants.py`、`crc.py`，以及 OpenMV AE3 固件板级配置中的 runtime VID/PID `37C5:16E3`。

## 10. 实测固件能力（2026-10-12 现场探测）

现场板卡不是通用 AE3 开发板，而是 **RA8P1 Titan Mini**：

```text
MicroPython v1.28.0-openmv-dd984559 on 2026-09-07; RA8P1 Titan Mini with R7KA8P1KF
```

固件注册的通道固定为 4 个，没有自定义通道：

| channel_id | 名称 | 用途 |
| ---: | --- | --- |
| 0 | `usb` | 原始 USB 通道 |
| 1 | `stdin` | 上传并执行脚本 |
| 2 | `stdout` | 设备 REPL / 脚本 `print()` 输出，host 可读 |
| 3 | `stream` | 视频帧 |

**该固件没有 `protocol` 模块**（`import protocol` → `ImportError`），因此脚本
**无法注册自定义 channel**，本文档前面设想的 `tracker` 控制通道在这块板子上无法实现。
`omv` 模块只提供 `arch`、`board_id`、`board_type`、`debug_mode`、`version_*`，没有任何通道 API。

可用模块（实测）：

- `csi`：相机采集，`CSI` 提供 `reset`、`pixformat`、`framesize`、`snapshot`、`flush`、
  `frame_callback`、`vsync_callback`、`window`、`framerate` 等。
- `sensor`：已废弃，官方提示改用 `csi`。
- `image`、`display`、`machine`、`ulab`、`micropython`、`gc`、`sys`、`time`、`utime`、
  `struct`、`ujson`、`json`、`vfs`、`os`、`cmath`、`lcd`、`uctypes`、`collections`、`omv`。
- `ml`：TFLite 推理入口，含 `Model`、`Workspace`、`Normalization`、`preprocessing`、`uml`。
- `ml.utils`：`NMS`、`threshold`、`dequantize`、`quantize`、`sigmoid`、`logit`、
  `draw_predictions`、`draw_skeleton`、`draw_keypoints`。

不存在的模块：`protocol`、`openmv`、`pyb`、`usb`、`usbserial`。

### SD 卡模型

根目录存放 OpenMV 量化模型（`u55` = Ethos-U55 NPU），人脸相关的主要有：

- `yolo_fastest_face_192_u55_256.tflite`：人脸检测，对应 UI 的 `yolo-fast-prototype` 配置。
- `blazeface_front_128_u55_256.tflite`、`blazeface128_u55_256.tflite`、`fomo_face_detection_u55_256.tflite`
- `face_landmarks_192_u55_256.tflite`
- 其他：`yolov8n_coco80_...`、`yolov8n_pose_...`、`movenet_singlepose_192_...`、
  `palm_detection_full_192_...`、`unet_mobilenetv2_128_...`、`vww96_...`、
  `resnet8_...`、`mobilenetv1_025_...`、`yolo_lc_192_...`、`swift_yolo_pet_192_...`。
  每个 `.tflite` 旁边通常有同名 `.txt`（模型元信息）。

### 已验证可用的链路

1. 视频：`csi.CSI()` + `RGB565`/`QVGA` + 循环 `snapshot()`/`flush()`，
   host 侧 `set_streaming(True)` 后可稳定读到 `320x240` JPEG 帧（每帧约 8 KB）。
   采集脚本不依赖 `protocol`，因此 `src/device/openmv_ae3_raw_jpeg.py` 已改为
   `try: import protocol / except ImportError: protocol = None` 的降级写法。
2. 设备 → host 文本：脚本 `print()` 的内容可从 `stdout` 通道读出
   （`CHANNEL_SIZE` + `CHANNEL_READ`），这是本项目目前唯一可用的设备主动上报路径。
   注意 `stdout` 通道缓冲区有限，一次性输出过长会被截断，需分次 `print` 并及时读取。
3. host → 设备：**只有 `stdin` 脚本上传这一条路**。要在设备上改变行为，
   需要重新下发脚本（可以把参数直接写进脚本文本），无法向运行中的脚本推送命令。

### host 侧取帧的两个坑（2026-10-12 修复）

1. **不要无条件轮询 `tracker` 通道。** 取帧线程原来每轮先 `read_channel("tracker")`，
   而本固件没有该通道，`read_channel` 直接抛 `OpenMVProtocolError`，被线程的兜底
   `except` 捕获后 `return`，**线程在读到第一帧前就退出了**，界面表现为“连接成功但没有图像”。
   现在 `_OpenMVFrameReader` 只在 `"tracker" in client.channels` 时才轮询事件，
   运行中若通道消失也只停事件、不停视频。
2. **单次异常不应终止取帧。** 现在连续 10 次解码/协议失败才上报 `read_error` 并退出，
   期间通过 `diagnostic` 上报 `OpenMV frame read failed (n/10)`，便于区分偶发抖动与真故障。

另外 host 侧的“启动/停止视频流”**不依赖控制通道**：`OpenMVSerialVideoSource.start()/stop()`
直接调用 `set_streaming(True/False)`（`STREAM_RAW_CTRL` + `STREAM_CTRL`），
设备据此推流/停流，冻结帧和续流都正常。因此 `start_stream` / `stop_stream`
这两个 JSON 命令即使送不到设备，视频功能也不受影响。
实测 320x240 JPEG 稳定在 ~16 fps（配置 15 fps），停流后再续流可恢复。

### 无帧看门狗（2026-10-12 新增）

现场出现过一次“连接成功但 90 秒无帧”的偶发情况（设备对所有 `CHANNEL_LOCK` 回 NAK `0x04`，
重新连接后即恢复），原代码对此既不自愈也无日志。现在 `_report_missing_video` 定时器（3 秒）
会检查帧计数是否前进，未前进则逐级恢复：

| 无帧时长 | 动作 | hardware.log 记录 |
| --- | --- | --- |
| 3 s | 重发 `STREAM_CTRL=1` + 重启取帧线程 | `watchdog: no new frames for 3s; re-asserting stream control` |
| 6 s | `STDIN_STOP` → 重传采集脚本 → `STREAM_CTRL=1` | `watchdog: still no frames; re-uploading the AE3 capture script` |
| 9 s | 停止看门狗并报错 | `watchdog gave up: ...` |

期间 UI 事件日志会同步提示用户正在恢复。只要画面正常（帧计数在涨），看门狗不做任何事，
也没有额外开销。若看门狗频繁触发，说明设备/线缆/供电有问题，需按 §9 进一步排查。

### 待决策

`mark_suspect` / `clear_suspect` 等控制命令的通道方案需要重定：
候选做法是把 ROI 等参数直接拼进下发脚本后重新 `STDIN_STOP -> RESET -> WRITE -> EXEC`
（代价是重启采集循环），或者另行约定一条设备轮询的信号路径。


### 帧率瓶颈定位与 host 侧优化（2026-10-12 晚）

现象：OpenMV IDE 同为 RGB565/QVGA 能跑 ~41 fps，本工程 UI 只有 ~16 fps。

真机分段测量结论（亮度不同时设备快照耗时有波动，数据取两次代表值）：

| 环节 | 实测 | 说明 |
| --- | --- | --- |
| 设备 `snapshot()` | 19-27 ms/帧 | 受自动曝光影响，设备上限约 33-41 fps |
| 设备 `flush()` | 3-5 ms | 固件将帧 JPEG 压缩后推入 stream 通道（帧头 pixformat=0x06060000） |
| host 旧版 CRC32 | **~26 ms/17KB** | 纯 Python 逐位循环，是 16 fps 的主要根因 |
| host 每帧 4 次命令往返 | ~22 ms 下限 | LOCK ~8 ms（设备响应延迟）+ SIZE 1.7 + READ ~10 + UNLOCK 2.3 |
| READ 内部 | 5 个分片 | 设备 `GET_CAPS` 上报 max_payload=4082，17KB 帧必分 5 片 |

已做修复（`openmv_protocol.py` / `hardware_video.py`）：

1. **CRC16/CRC32 查表化**：256 项表驱动，结果与逐位实现逐位一致（随机数据 + 随机初始值
   对拍验证）。17KB 帧校验从 ~26 ms 降到 ~3.9 ms。这是 16 fps -> 27 fps 的主因。
2. **`_receive_packet` 只在无新字节时睡眠**：原来每次迭代固定 `sleep(1ms)`，
   多分片帧接收期被拉长 5-10 ms。
3. **取帧线程 NAK 轮询 5 ms -> 2 ms**。

当前水位：raw client ~27 fps，UI 真实链路 ~25 fps、0 错误。

已排除的方向（勿重试）：

- **跨帧保持 LOCK**：设备拒绝（NAK status 0x01），协议要求每帧一次 LOCK 事务。
- **加大 max_payload**：设备硬上限 4082，`SET_CAPS` 提高了也没用。
- **slicing-by-4 CRC**：实现验证可行（等价），但纯 Python 下仅再快 ~18%（循环开销为主），不值得复杂度。

若要继续逼近 41 fps，可选方向在设备侧：降低 JPEG `quality`（`csi` 实例有该属性）
使单帧变小、减少分片数；或固件支持更大的 max_payload / 合并 LOCK-SIZE-READ 事务。

### 设备端人脸检测与 stdout 上报（2026-10-13 实现）

`src/device/openmv_ae3_raw_jpeg.py` 现在是**单文件自包含**的设备端程序：可在 OpenMV IDE
直接运行，也可由 host 原样上传执行（host 不生成、不改写脚本文本）。

#### 与 host 的全部契约（仅两条）

1. **帧**：host 保持 `STREAM_CTRL=1`，固件自动把 CSI 帧缓冲推到 `stream` 通道。
   脚本只需持续 `snapshot()`。**`image.flush()` 不是必需的**（真机实测有/无 flush
   均为 ~22fps 且画面实时；此前文档写需要 flush 是不准确的）。
2. **检测结果**：脚本周期性 `print()` 一行 JSON 到 stdout，
   host 从 `stdout` 通道轮询读取（`CHANNEL_SIZE` + `CHANNEL_READ`）。

   ```json
   {"kind": "event", "name": "tracks_update", "payload": {"tracks": [
       {"track_id": 1, "bbox": [x1, y1, x2, y2], "confidence": 0.93, "label": "face"}],
       "frame_size": [320, 240], "fps": 41.7}}
   ```

   bbox 为 `(x1,y1,x2,y2)`，与 UI `Detection.bbox` 一致，host 无需换算。

#### 模型选择（重要，勿再踩）

- **`yolo_fastest_face_192_u55_256.tflite` 不可用**：输出为两个 YOLOv5 风格头
  `(1,6,6,18)` / `(1,12,12,18)`（18 = 3 锚框 × (4 框 + 1 obj + 1 类)），
  而本固件的 `ml.postprocessing.ultralytics.YoloV5` / `YoloV8` 解码时都在
  `ultralytics.py` 内部抛 `ValueError: too many values to unpack (expected 3)`
  （YoloV5 在 line 51，YoloV8 在 line 106），与传入的张量个数无关。
  该模型的锚点表也不在配套 `.txt`（只有 `face`）里，自写解码器缺锚点。
- **实际使用 `blazeface_front_128_u55_256.tflite` + `ml.postprocessing.mediapipe.BlazeFace`**：
  官方配套后处理器，真机 4 个输出张量有真实信号，推理几乎零开销。
- `ml` API：`ml.Model(path, postprocess=BlazeFace(threshold=0.5))`，
  `model.predict([img])`，结果为 `(rect, score, keypoints)` 元组列表；
  `model.input_shape/output_shape/labels` 可查张量布局。设备 `sys.path` 含 `/` 与 `/lib`，
  SD 卡上的 `.py` 可被 import（已实测）。

#### 采集与推理必须解耦

真机实测：设备每帧都跑推理时，host 只能收到 ~16fps；采集与推理解耦后恢复：

| 设备脚本 | host 实收 |
| --- | --- |
| 每帧推理 | 16 fps |
| 每 2 帧推理 | 21 fps |
| 每 3 帧推理（当前，DETECT_EVERY=3, REPORT_EVERY=1） | **22 fps**，检测框 ~14 Hz |
| 不推理 | 25 fps |

host 侧 stdout 轮询间隔 50ms（`_OpenMVFrameReader.STDOUT_POLL_INTERVAL`）；
实测 20ms 与 50ms 对帧率无影响，50ms 更省串口带宽。

#### host 侧改动

- `_OpenMVFrameReader` 事件轮询通道从（不存在的）`tracker` 改为 **`stdout`**，
  JSON 行解析失败（REPL 横幅/traceback）直接跳过；`tracks_update` 转成
  `Detection` 列表随帧一起发出（`frame_received(image, detections)`）。
- `detections` 语义：`None` = 设备尚未上报过（不清空既有叠加层）；
  `[]` = 画面里没有人脸（清空叠加层）。
- `VideoDisplay` 新增 `set_detections()`：绿框 + `label score` 标签画出每个检测；
  嫌疑人红框仍画在最上层。

## 11. 行人检测与实时点选锁定（2026-10-13）

### 需求变化

人脸检测无法覆盖「行人背对/侧对镜头」的场景，嫌疑评估必须基于全身行人检测。
同时「暂停画面 → 圈选 ROI → 恢复推流」的工作流太慢，解冻时人已走出视野，
所以改为**实时点选锁定**，全程不暂停画面。

### SD 卡可用行人模型（2026-10-13 逐个真机验证）

| 模型 | 大小 | 输出 | 解码器 | 单次推理 |
| --- | --- | --- | --- | --- |
| `swift_yolo_person_nano_192_u55_256.tflite` | 1.6 MB | `(1,2268,6)` 单类 person | YoloV5 ✅ | **18 ms** |
| `yolov8n_coco80_192_u55_256.tflite` | 2.1 MB | `(1,84,756)` COCO 80 类 | YoloV8 ✅ | 33 ms |
| `yolov5n6_xiao_voc20_192_u55_256.tflite` | 546 KB | `(1,2295,25)` VOC 20 类 | YoloV5 ✅ | 44 ms |

三个都能被固件自带解码器正常解码（无 `yolo_fastest_face` 那种双头不兼容问题）。
选用 `swift_yolo_person_nano`：最快、单类即所需。空场景实测 54 fps（含每帧推理）。

### 新工作流：实时点选锁定（无下行依赖）

1. 设备端脚本持续检测**所有行人**，设备内简单 IoU 跟踪器（贪心匹配，
   `IOU_MATCH_THRESHOLD=0.25`，`MAX_MISSES=8`）为每个行人维护稳定 `track_id`，
   经 `stdout` JSON 行上报（与原 `tracks_update` 格式一致，`label="person"`）。
2. 操作员在**实时画面上直接点击**某个行人框 → `VideoDisplay.suspect_picked(track_id)`
   → 主窗口锁定 `suspect_track_id`，红框跟随、轨迹记录、自动模式联动录像。
3. 嫌疑人短暂丢失时保持锁定并等设备跟踪器重新匹配（只记一次日志）；
   长时间不恢复提示重新点选。

**该方案刻意不依赖下行通道**：设备不知道谁是嫌疑人，只报所有行人；
「谁是嫌疑人」由 host 侧锁定 track_id 决定。这绕开了本固件无自定义通道、
`mark_suspect` 无法下发的限制，也满足「画面永不中断」硬约束。

### 设备脚本参数（`src/device/openmv_ae3_raw_jpeg.py`）

- `DETECT_EVERY=2`、`REPORT_EVERY=2`：24 fps 采集下检测 ~12 Hz、上报 ~6 行/s，
  stdout 不会反压采集循环（真机 8 秒 49 行、无异常）。
- YoloV5 结果元素为 `(x, y, w, h, score[, class])`，脚本按索引取值，
  兼容 5/6 元组；bbox 已由固件缩放回输入图像坐标（320x240）。

### 待真机确认

- 真人（背面/侧面、多目标遮挡）检出率与 track_id 稳定性尚未验证（探测时镜头前无人）。
- 若 4:3 → 1:1 模型输入的形变影响检出率，可尝试方形窗口采集再裁剪。

### 临时探测脚本

- `probe_sd_models.py`：列出设备 `/` 与 `/lib` 文件（逐行打印避让 stdout 环形缓冲）。
- `probe_person_models.py`：批量加载候选模型，打印输入/输出张量与推理耗时。
- `probe_person_live.py`：行人模型实时检出测试（12 秒窗口）。

### 11.1 修正：最终采用 coco80 + 官方后处理器（2026-10-13 深夜）

§11 初版的 swift_yolo_person_nano 方案在真机测试中暴露致命问题，已回退：

- swift 模型输出 (1,2268,6) 是**已解码**的 (cx,cy,w,h,obj_logit,cls_logit)，坐标为 ~1% 量化单位
  （0~99，步长 2.09），固件 YoloV5/YoloV8 后处理期望原始 grid 回归值，二次解码出垃圾 → 检测恒空。
  yolov5n6_xiao_voc20 同理。自解码虽能出框，但坐标空间（192/208/百分比）无法确证，
  且镜头离分布（黑屏）时该模型输出大量 conf≈1.0 的全屏假框。
- **最终方案：`yolov8n_coco80_192_u55_256.tflite`（33ms）+ 固件官方 `YoloV8` 后处理器**。
  其 (1,84,756) 通道优先张量正是官方文档「YOLOv8 walkthrough」描述的格式：
  前 4 通道 xywh 归一化 [0,1]、后 80 通道类别分数 [0,1]。后处理器直接返回
  **图像空间的 (x,y,w,h)**，无任何坐标换算歧义。
- **检测条目格式（真机确认）**：`predict()` 返回按类别组织的列表，`results[0]` 即 person 类，
  每项为 `([x, y, w, h], score)`（注意 rect 是列表，不能 float(d[0])）。
- 真机验证：测试视频场景红框精准套在行人身上，设备端 IoU 跟踪器给出稳定 ID1/ID2，
  DETECT_EVERY=2 下 fps 16~19（swift 方案是 24 但解码不可用）。
- 调试方法论（以后排查模型输出问题直接用）：
  1. 设备上报原始张量热行（probe_debug_person.py 思路）；
  2. host 端「帧+检测时间同步配对」后按候选坐标系离屏叠加 PNG 肉眼对位
     （probe_verify_sync.py）——注意把框画进 snapshot 帧随流推送**不可行**（推流发生在
     snapshot 时，绘制赶不上推送）；
  3. 优先怀疑「固件后处理器 ↔ 模型输出布局」不匹配，参考官方 walkthrough 确认布局。

## 12. UI 改版：点选锁定、录像、帧率与分辨率（2026-10-14）

### 12.1 嫌疑人红框丢失问题（已修）
现象：锁定嫌疑人后，人还在画面里红框却消失。
根因有两层：
1. 设备端 IoU 跟踪器在遮挡/快速移动后会**派发新的 track_id**，host 仍按旧 ID 找人 → 找不到；
2. 设备端偶发漏检一两拍，host 立即清掉跟踪框。

修复（host 侧，`main_window.py`）：
- 记录嫌疑人最近已知框 `_last_suspect_bbox`；
- 锁定 ID 消失时，在最新检测里找与该框 **IoU ≥ 0.3** 的最大重叠者，自动把锁定**重新绑定**
  到新 ID（日志 `suspect_relocked`，只报一次）；
- 短暂丢失时红框**保持显示最近已知框 6 个检测周期**（宽限期），超时才撤框；
- 锁定本身永不自动清除，等设备跟踪器重新匹配或操作员重新点选/清除。

### 12.2 自动录像（保存时机 + 不生成文件）
**保存时机**：OpenCV 的 MP4 索引只在 `writer.release()` 时写入，所以录像过程中文件虽已存在
但**不可播放**。自动跟随模式（`recording_mode == "automatic"`）下录像是这样结束的：
1. 点击「清除标记」→ `_stop_recording()`，保存 MP4；
2. 关闭程序 → `closeEvent` 保存并记日志；
3. 嫌疑人连续丢失 `AUTOMATIC_RECORDING_STOP_SECONDS`（默认 15 秒，1 秒粒度看门狗
   `_check_automatic_recording`）→ 自动结束并保存，日志写明路径。
   **锁定本身不会被自动清除**，操作员回来后重新点选即可开启下一段录像。

**不生成文件的两个根因（已修）**：
- 录像输出用的是**相对路径** `recordings/`，依赖进程 CWD；打包后用 `pythonw` 启动时 CWD
  不是程序目录，MP4 写到了别处；
- `VideoRecorder.write()` 原来不校验尺寸：OpenCV 对尺寸不符的帧**静默丢帧**，结果空文件被
  清理逻辑删除。
修复：`paths.recordings_directory()` 绝对路径 + `write()` 尺寸不符时 resize + 代码见 §12.5。
**红框烧录**：录像帧经 `video.annotate_frame()` 绘制所有检测框 + 红色 suspect 框与
`suspect ID n` 标签后再写 MP4（离屏抽帧验证：红色像素 2458）。

### 12.3 帧率与分辨率
- 状态栏新增 `帧率：上位机 X fps ｜ 设备采集 Y fps`：host fps 由取帧线程 1 秒窗口统计，
  device fps 来自设备脚本 `tracks_update` 上报的 fps 字段；
- 原「AI 配置」下拉框（多模型测试遗留）改为**采集分辨率**选择：QVGA 320x240（默认）/
  VGA 640x480，连接时通过 `_script_text()` 字符串替换进采集脚本上传后生效。
  VGA 画面更大但帧率约减半（实测 15.6 → 8.0 fps 带检测）；
- 传感器仅支持这两档（HVGA 被拒绝），详见 §11。

### 12.4 其它 UI 调整
- 删除「标记嫌疑人」「标记名称」按钮/输入框（实时点选已全自动标记），保留「清除标记」；
- 右侧「实时目标 / 轨迹路径 / 命令与事件日志」改为可拖拽的垂直 splitter，高度充足；
  目标表 bbox 列 Stretch，不再截断；
- 点选嫌疑人后红框立即显示（不必等下一次检测上报）；
- 控制区新增「打开输出目录」按钮（`QDesktopServices` 打开程序目录）。

### 12.5 输出目录策略与日志本地化（2026-10-14）

**所有输出都在「程序目录」下**，实现集中在 `src/suspect_tracker/paths.py`（只依赖标准库，
所以启动器在 cv2/PySide6 导入失败时也能写日志）：

```
<程序目录>\log\ui_events_YYYY-MM-DD.log      命令与事件日志（每天一个文件）
<程序目录>\log\hardware_YYYY-MM-DD.log      OpenMV 协议 trace
<程序目录>\log\startup_YYYY-MM-DD.log       启动分阶段日志
<程序目录>\recordings\suspect_YYYYMMDD_HHMMSS.mp4
```

- **程序目录的判定**：打包运行时取 `Path(sys.executable).parent`（onefile 构建的 EXE 在
  `dist\`，输出就落在 `dist\log`、`dist\recordings`）。**不能用 `sys._MEIPASS`**——onefile
  的 `_MEIPASS` 是临时解压目录，退出即删。源码运行时程序目录 = 工程根。
- **回退**：程序目录不可写（例如装到 `Program Files`）时回退到
  `%LOCALAPPDATA%\TitanSuspectTracker`，用写探针 `.write_probe` 判断，结果缓存。
- **日志按天分文件**：文件名带 `YYYY-MM-DD`，跨天自动新文件（原来是单个 `ui_events.log`）。
- **日志本地化**：UI 文案全部走 `i18n.text()`，跟随语言切换。OpenMV 驱动层仍用英文发诊断
  （它同时写 protocol trace），UI 侧用 `i18n.diagnostic_text()` 按正则表重渲染已知消息
  （连接/首帧/看门狗/通道不可用等 10 条），未知消息原样透传；`hardware.log` 保持英文原文
  便于和驱动日志对照。

## 13. 锁定稳定性与交互收敛（2026-10-14 晚）

### 13.1 移除「停止视频流后圈选嫌疑人」（用户要求）
- 停止视频流后原来会启用冻结帧拖拽框选：设备已停流、不会有新检测，框选**无法锁定任何人**，
  画出的黄框还会**滞留在画面上**且没有按钮能清除。
- 已整体删除该工作流：`VideoDisplay` 的 `selection_changed`/`set_selection_enabled`/拖拽
  状态与黄色选框绘制全部移除；`main_window` 的 `selected_roi`/`_handle_roi_selection` 一并删除。
- 现在唯一的选择方式 = **推流时点击画面中的行人**；停止视频流时点击无效（`_handle_suspect_picked`
  直接 return），提示文案改为「视频流已停止；点击『启动视频流』并点击画面中的行人即可锁定嫌疑人」。
- 点击命中改为**就近吸附**：点击点距检测框边缘 ≤ max(6px, 0.35×短边) 即命中（取最近的框），
  远处小目标也容易点中。
- 连带清理：`_start_recording()` 删除 `allow_paused`（录像必须有活流，否则只会产出空文件）；
  停止视频流时若有录像在录，立即结束并保存（否则 MP4 索引永远不会写入）。

### 13.2 多人靠近时红框跳到旁人（已修，`suspect_lock.py` 新模块）
根因：host 侧重绑定只看「与最近已知框 IoU ≥ 0.3 的最大者」。设备端 IoU 跟踪器在遮挡/快移/
两人交叉时会派发新 ID 或把旧 ID 指到另一人身上；此时最近已知框是滞后的，站在原地的旁人
往往比走开的嫌疑人重叠更大 → 红框跳人。

新模块 `src/suspect_tracker/suspect_lock.py`（纯 Python/OpenCV，无 Qt，可离线测试）：
1. **运动预测**：最近框按指数平滑的位移外推，参考框跟着走的人而不是滞后在原地；
   候选按 `0.7×IoU + 0.3×中心接近度` 对预测框打分。身份跳变（位移 > 0.5 身位）时清零速度估计。
2. **外观门控**：嫌疑人可见期间维护躯干区 HSV 直方图签名（EMA 缓更新）。
   相关系数 < 0.45 的候选按 0.25 折扣——**折扣上限 0.25 故意低于重绑定门限 0.30**，
   因此「外观不像嫌疑人的人」永远拿不到锁定（这是防跳人的关键不变量）。
3. **拒绝猜测**：新 ID 必须**连续 2 拍**胜出才移交锁定；两个候选得分接近（< 1.15 倍）时
   不做选择，红框保持最近已知位置等下一拍，日志提示一次「多个相近目标」。
4. **交叉换人纠正**：ID 仍在但外观明显不符（<0.45）而另一候选外观强匹配（≥0.60 且高出
   ≥0.20）时，无视（已被带偏的）运动预测、按外观纠正，同样要过 2 拍确认。
5. 锁定永不自动清除；短丢失宽限 8 拍（~1s）内红框保持最近已知位置，超时撤框但锁定保留，
   人回来自动恢复并记 `suspect_reacquired`。

测试 `probe_suspect_lock.py`（无 Qt）：20 项覆盖上述全部场景，含「旁人站在嫌疑人原位不能
接管锁定」「交叉后 ID 指错人被纠正」「双胞胎歧义不跳框」。
局限（如实说明）：两人框高度重叠时躯干区互相污染，直方图无法区分 → 此时宁可保持不动，
等人分开后由外观门控纠正。

### 13.3 录像只烧红色嫌疑人框
`video.annotate_frame(frame, suspect_box, ...)` 重写：只画红框 + `suspect ID n` 标签
（ASCII，cv2.putText 不支持中文），**不再画其他行人的绿框**（用户要求，避免干扰视觉）。
实时画面仍显示全部绿框 + 红框（点选需要）。录像文件名同秒冲突改为 `_2`/`_3` 后缀去重。

## 14. VGA 下红框不跟人：根因与修复（2026-10-14 深夜）

### 14.1 现象与定位（结论：不是上位机性能问题）
用户反馈：点选左下方行人后红框不跟随。现场日志（log/ui_events_2026-10-14.log）还原：
```
22:17:45 已锁定嫌疑人 ID 4   → 22:17:55 ID 4 离开画面（不再出现）
22:18:06 已锁定嫌疑人 ID 35  → 22:18:07 离开（1 秒！）
22:18:10 ID 42 → 22:18:11 离开；22:18:15 ID 52 → 16 离开
22:18:21 ID 63 → 28 离开 → 重绑 87 → 29 离开；22:18:54 ID 151 → 55 离开
```
每次点选后 1 秒内锁定的 ID 就从上报中消失——快于任何上位机处理延迟，排除性能瓶颈。
真正根因：**VGA 下设备端 IoU 跟踪器疯狂派发新 ID**（70 秒内 4→35→42→52→63→87→151），
host 锁住的 ID 频繁"死亡"；而 host 重绑定要求"同一新 ID 连续 2 拍胜出 + 与预测框重叠
≥0.30"，ID 轮换快于上报率时确认永远完不成 → 红框停留在最后已知位置（宽限期画旧框），
看起来就是"没跟上"。

为什么 QVGA 没事、VGA 崩：VGA 采集帧率减半（11.8fps），DETECT_EVERY=2 → 检测间隔翻倍，
行人两拍之间位移更大；远处行人框很小（35x45），位移后 IoU 掉到 0.25 阈值以下 → 设备
派发新 ID。检测评分在阈值附近波动（闪烁）进一步加剧换 ID。

### 14.2 设备端修复（src/device/openmv_ae3_raw_jpeg.py）
跟踪器从"纯 IoU 贪心匹配"升级为"速度预测 + 混合评分"：
1. 每个 track 维护指数平滑的中心速度（vx,vy）；匹配时先按速度**外推预测框**再算 IoU；
2. IoU 之外并行计算**中心距离门**（0.9×框平均边长，按框大小自适应），两者取较大分——
   部分重叠（IoU 0.1~0.2）不再直接判负；fallback 分数上限 0.5，永远不会压过真实重叠；
3. 位移超过 1.2×边长才视为身份跳变并清零速度（过严会让快速走动永远无法建立速度估计，
   host 模拟实测发现）；
4. `REPORT_EVERY` 2→1：VGA 上报率 ~3Hz → ~6Hz，host 拿到全部检测（一行 JSON ~180 字节，
   stdout 环形缓冲无压力）。
跟踪器代码用 `# --- track-keeper block (host-testable) ---` 标记圈出（纯 Python 无硬件
依赖），`probe_device_tracker_sim.py` 直接 exec 这段真实代码做模拟：普通走动/小框快速
走动/漏检两拍/两人交叉/距离门边界，6 项全 PASS。

### 14.3 host 身份保持器修复（suspect_lock.py）
1. **单候选快速确认**：重绑定时若只有一个候选高于门限（第二名低于门限），第一拍立即
   接受；两个候选竞争（都高于门限且分值接近）时仍保留连续 2 拍确认窗口；
2. **宽限期** 8→12 拍（VGA 上报率下约 2 秒红框保持最后已知位置）；
3. **纯外观重捕获**：宽限过期后（运动预测已失效），外观强匹配（≥0.60）且几何上仍合理
   （≥0.12）且**唯一**的候选可立即重获锁定——覆盖"嫌疑人走远后回到画面别处"的场景。
`probe_suspect_lock.py` 24 项（新增 3b 竞争确认窗口、3c 逐拍换 ID 场景）+ `probe_ui_smoke.py`
全 PASS。日志文案 `suspect_relocked` 改为"身份已重新确认"。

### 14.4 遗留观察项
- 设备端换 ID 频率需真机复测（预期大幅下降；遮挡超过 MAX_MISSES=8 检测拍 ~1.4s 仍会换 ID，
  此时 host 靠 14.3 的快速重绑定兜底）；
- 若真机仍偏保守/偏激进，调节旋钮：设备端 `IOU_MATCH_THRESHOLD`、fallback reach（0.9×边长）；
  host 端 `relock_floor`(0.30)、`ambiguity_ratio`(1.15)、`REACQUIRE_GEOMETRY`(0.12)。

## 15. 单实例运行（2026-10-14 深夜）

### 15.1 需求与方案
现场只有一块 Titan Mini 板卡，同时开两个上位机只会互相抢 COM 口（第二个窗口必然连接失败），
所以程序改为**单实例**：第二次启动不再开窗口，而是把已在运行的窗口提到前台并提示。

实现放在新模块 `src/suspect_tracker/single_instance.py`，分两层：
1. **Windows 内核互斥体**（`CreateMutexW`，纯 `ctypes`，零 Qt 依赖）—— 决定谁拥有实例。
   选它而不是锁文件的原因：进程被 `taskkill /F` 强杀时由内核自动回收，不会留下需要清理的
   陈旧锁；而且**源码运行与打包 EXE 用同一个名字**，所以开发版和发布版也互斥。
2. **命名管道**（`QLocalServer`/`QLocalSocket`）—— 只负责"把已有窗口叫到前台"。
   Windows 上管道是内核对象，进程结束即消失，不落磁盘。

检查点在 `__main__._run()` 的**最前面**（早于 PySide6 导入）：
- 抢到互斥体 → 正常启动，创建 QApplication 后 `start_server()` 监听管道；
- 抢不到 → 向管道发一条 `activate`，弹一个中英双语提示框，退出（返回码 0）。

第一个实例收到 `activate` 后调用 `MainWindow.activate_from_duplicate_launch()`：
`showNormal()`（若已最小化）+ `raise_()` + `activateWindow()` + `QApplication.alert()`
（Windows 常拒绝后台进程抢焦点，任务栏闪烁是必要的兜底），并在事件日志记一条。

### 15.2 两个实现陷阱（都已在真机验证暴露并修复）
1. **`release()` 必须真的关句柄**：内核互斥体只要还有**任何**句柄打开就是"存在"的，
   包括本进程自己那个。原先 `release()` 只清状态不 `CloseHandle`，导致
   "释放后立即重新获取"永远失败（跨进程测试直接抓到）。
2. **`GetLastError` 必须先用 `SetLastError(0)` 清零**：Win32 约定**成功时不写 last error**，
   于是 `CreateMutexW` 成功后读到的是上一次残留值。若残留恰好是 `ERROR_ALREADY_EXISTS(183)`
   （例如刚查过一个真存在的互斥体），**全新启动会被误判为"已有实例"而拒绝启动**。
   这是最危险的一个 bug，现已 `ctypes.set_last_error(0)` 前置。

### 15.3 验证
`probe_single_instance.py`（用独立的互斥体/管道名，不影响真机运行的程序）覆盖：
同进程二次获取被拒、无管道时通知快速失败（不阻塞）、**真实跨进程**（子进程持锁并被激活
管道唤起）、持有者被强杀后锁自动释放（带重试计时，实测为立即释放）。
11 项全 PASS。EXE 双开实测：第二次启动的 startup 日志为
`Another instance is already running; handing over and exiting` +
`Activation request delivered to the running instance: True`，
第一个实例的 ui_events 出现"检测到重复启动：程序只允许运行一个实例，已将当前窗口置于前台。"

### 15.4 排障记录：一次由启动方式造成的假阴性
首次验证是用 `cmd //c start "" dist\TitanSuspectTracker.exe` 连开两次，结果**两个窗口都正常
启动**，连第二次的 `CreateMutexW` 也返回 `last_error=0`（看起来"打包后互斥失效"）。真实原因
不在代码：这种启动方式让两次 `start` 落进了**不同的内核命名空间**（执行环境隔离），同名内核
对象互不可见——用同一环境里的探针进程去 `CreateMutexW` 同一个名字，拿到的也是"全新创建"。
换回与被测进程相同的启动方式（直接运行 `dist\TitanSuspectTracker.exe`）后，设计行为立即复现：
第二次 `CreateMutexW` 返回 `last_error=183`（`ERROR_ALREADY_EXISTS`）→ 通知 →
`Activation request delivered: True` → 退出。**教训**：验证内核对象、命名管道这类依赖命名空间的
机制时，必须先保证启动方式与被测进程一致，否则得到的是环境假阴性，而不是代码结论。

顺带改进：`SingleInstance` 现在接收一个 `log` 回调（`__main__` 传启动日志写入函数），
把 `CreateMutexW` 的句柄与错误码、以及"互斥体不可用只好放行"的情况**落到 startup log**，
以后再遇到双开问题可以直接看日志定位，不必靠猜。

## 16. 脱离上位机的独立运行（2026-10-16）

目标：板卡 + 800x480 LCD + SD 卡，不接 USB 自行完成行人检测、嫌疑人锁定、UI 显示、
日志与录像。已全部打通并真机验证。

### 16.1 固件能力实测（probe_device_standalone.py / probe_standalone_timing.py）

- **没有 `lvgl` 模块**（Kconfig 与源码均无，仅 Alif DevKit-e7 的 C 模板）→ UI 不能用
  LVGL，改用 `image.draw_*` 自绘。签名注意：本固件 `draw_string/draw_rectangle/draw_line`
  的坐标一律传**元组**（`(x, y)` / `(x, y, w, h)`），散参数报错。
- `lcd` 是 `display.RGBDisplay` 的薄封装，固定 800x480@56Hz 双缓冲。
  **实测时序（QVGA 基准 41fps）**：`lcd.display(img, scale=2)` 写视频区**零开销**
  （41.3fps）；第二次 `lcd.display(panel, x=640)` 写状态栏掉到 29.8fps → 面板改为
  每 `PANEL_EVERY=5` 帧刷新一次。
- `mjpeg.Mjpeg` 可用，`write()` 开销 ~2%。**文件头是 RIFF —— 实际生成标准 AVI 容器**，
  VLC 直接播放。
- **没有 `machine.RTC`**，`time.localtime()` 从 32 位纪元起算（2106 年）→ 日志时间戳
  只能用开机相对秒，文件名用 `/tracker/boot_count.txt` 开机计数区分。
- SD 可读写（`os.mkdir`/`open('a')`/`os.remove` 均正常）；`os.statvfs` 不可用。
- `draw_string` 输出探测见 §16.3 布局。

### 16.2 独立应用 `examples/titan_standalone_tracker.py`

- 管线：QVGA 采集 → coco80 检测（DETECT_EVERY=2）→ IoU 跟踪器（与 USB 脚本同一
  track-keeper 块）→ 设备版嫌疑人保持器 → 绘制 → LCD + mjpeg + SD 日志。
  实测整体 **13~18 fps**（检测+LCD+录像全开）。
- 布局 800x480：左 640x480 视频（QVGA×2，绿框=旁人、红框=嫌疑人），右 160x480 状态栏
  （FPS/人数/状态/嫌疑人号/REC 秒数 + 最近 10 条日志 + BOOT 序号）。
- **嫌疑人保持器（设备版）**：ID 消失 → IoU≥0.3 重绑新 ID（真机抓到 1→2 IoU=0.95）；
  宽限 12 个检测拍保持最后位置；过期 STATE_LOST 藏框但**永不自动清除**，之后任意一拍
  重新出现重叠 ≥0.3 即重锁。触摸驱动就绪后只需调 `select_suspect(track_id)`。
- **触摸前的临时选择**：`AUTO_PICK=True` 自动锁画面中心最近的人（让全链路先跑通）。
- **录像策略**（对齐上位机自动模式）：锁定即开始录像；**先画红框再 write、再画绿框**
  → 录像文件里只有红色嫌疑人框；连续丢失 15s 停止；单文件上限 300s 自动滚动。
- **SD 输出**：`/tracker/log/bootNNN.log`（事件日志，`[+开机秒] 消息`）、
  `/tracker/rec/bootNNN_MM.avi(mjpeg)`、`/tracker/boot_count.txt`。
- **开发者钩子**：`DEV_SECONDS`/`DEV_SNAPSHOT_AT_S` 非 0 时限时运行并把视频帧与
  状态栏各存一张 JPEG 经 stdout base64 回传（probe_standalone_run.py 重组为
  `pic_dev/snapshot_*.jpg`）。分块要 sleep_ms(8) 节流并暂停遥测，否则 stdout 环形
  缓冲冲掉数据（实测教训）。日常运行两值均为 0。

### 16.3 开机自启（install_standalone.py）

- SD 根目录 `/main.py` 写一行 `import titan_standalone_tracker`，应用本体放在
  `/titan_standalone_tracker.py`，**上电即自动运行，完全不需要 USB**（真机
  machine.reset() 后验证：自动锁定+录像+15fps 遥测）。删除 `/main.py` 即回到 REPL。
- 安装方式：板卡协议没有文件下行能力，`install_standalone.py` 把应用文本 base64
  内嵌进安装脚本经 stdin 上传、由板子自己写文件（~25KB 脚本上传没问题）。
- **坑**：`machine.reset()` 必须放在脚本里先 `time.sleep_ms(800)` 再执行——直接在
  run_script 命令里 reset 会把协议交互打断报 0x06，且 reset 是否生效无法确认。
- 停止运行：上位机协议 `stop_script`、或 OpenMV IDE 连接、或直接断电。
  注意无人值守时会持续录像（~170KB/s），长期挂机记得清卡或关 AUTO_PICK。

### 16.4 整屏闪烁的根因与修复（2026-10-16 深夜）

- **现象**：独立应用上屏后整个屏幕剧烈闪烁。
- **根因（读 titan_display.c display_write()）**：该驱动**没有局部更新能力**——每次
  `write()` 都先把「被绘制矩形之外」的整个帧缓冲 memset 成黑（上方/下方整行 + 覆盖行的
  左右两侧），再画矩形内容，然后 `titan_lcd_present()` 换缓冲。因此"视频区每帧写一次 +
  面板每 5 帧写一次"的两路局部写会互相抹黑：写视频时面板区域变黑、写面板时视频区变黑，
  两半屏轮流熄灭——正是闪烁的来源。用户猜测的"全屏重绘"方向反了：**恰恰是局部写造成的**。
- **修复**：一帧只 present 一次。建一块 800x480 RGB565 画布，每帧把 QVGA 帧 2x blit 进
  画布（`canvas.draw_image(frame, 0, 0, x_scale=2, y_scale=2)`），面板每 PANEL_EVERY 帧重画
  后 blit 到 (640,0)（画布自身保留两次刷新之间的面板内容），最后 `lcd.display(canvas)`
  一次写满整屏。首次进入主循环前先渲染一次面板，避免第一帧出现画布未初始化像素。
- **实测（probe_standalone_compose.py，QVGA 基准 41fps）**：两路局部写 29.6fps；
  每帧合成整屏 34.7fps；**合成+面板缓存 41.3fps（= 完全不写屏）**，2x blit 本身
  41.5fps（零成本）。带检测+录像的整体帧率与修复前持平（14~18fps）。
- （2026-10-16 深夜更正：此前判断"dev 快照中央的第二块面板是相机拍到 LCD"是**误读**——
  用 OpenCV 切片核实，那其实是画布 x≥640 处真正的状态栏，左侧只是很暗的桌面画面
  （亮度均值 4.8/255）。摄像头并没有对着屏幕，不存在反馈画面，摆放无问题。）
- 复现安装注意：install_standalone.py 重启后的验证泵偶尔赶不上应用启动输出（竞态），
  以 reset 后重新连读 stdout 的 telemetry 为准。

### 16.5 在线/离线共存与文字可读性（2026-10-16 深夜，真机验证）

- **USB 在线模式与离线自启完全兼容**：host 的 `run_script()` 先发 `IOCTL_STDIN_RESET`
  （VM 软复位）→ 正在跑的离线应用被打断 → 上传在线脚本接管。实测：离线应用自启运行中
  （boot #11，锁定+录像），host 用真实采集脚本接管，`stream` 通道 102 帧/6.1s=16.8fps 正常。
- **接管时录像被正常收尾**：软复位以 KeyboardInterrupt 打断脚本，`finally` 确实执行——
  boot010/boot011 两份日志都有 `rec_stop ... (shutdown)` + `shutdown`，AVI 不会留下截断文件。
- 注意：USB 会话结束后离线应用**不会自动恢复**（main.py 只在开机执行），重新上电即回离线模式；
  被接管瞬间 LCD 会熄灭（finally 里 lcd.deinit()，在线模式不上屏）。
- 新增 `LOCK_CAMERA`：开机 settle 后锁死曝光/增益/白平衡（实测生效：exposure=17611us
  gain=18db），消除暗光下 AGC 找目标造成的整幅明暗脉动；全部 try/except 保护。
- 面板关键读数（FPS/PPL/ST/ID/REC）改 scale=2，真机快照确认清晰可读；日志仍 scale 1。
- 新增 `BUILD_TAG`（面板+启动日志+遥测 JSON 三处都带），一眼确认 SD 上跑的是哪一版。
- 新增 `probe_lcd_static.py`：无相机的静态色条/黑白交替全屏测试，用于把"屏侧闪烁"与
  "相机侧闪烁"分开判定（人眼观察）。
- 新增 `probe_usb_takeover.py`：完整复现"离线自启 → USB 接管 → 日志核验"链路。
- SD 根目录遗留 `ae3_probe_mod.py`（早期调试残留），待清理。

### 16.6 闪烁排查续：静态不闪 + 三段分离测试（2026-10-16 深夜）

用户观察：**静态全屏色条/黑白交替完全不闪** → 显示路径（GLCDC/双缓冲/present）无罪。
用户更正：摄像头没有对着屏幕 → 排除亮度反馈环。此时剩余嫌疑只有"实时管线"本身。

`probe_flicker_matrix.py` 三段分离测试（每段 12s，面板显示当前段名，曝光已锁）实测：

| 段 | 内容 | fps | 帧间隔 avg/max |
|---|---|---|---|
| P1 | 只有实时画面（合成+present） | 29.7 | 33.6 / 36 ms |
| P2 | + 行人检测（每 2 帧推理+画框） | 14.2 | 70.6 / 86 ms |
| P3 | + mjpeg 录像（SD 写入） | 11.2 | 88.8 / 118 ms |

帧间隔 max 都贴近 avg、无恶性尖峰 → **带宽竞争致闪的假设基本排除**。
最可能的解释收敛为：用户报告的闪烁来自**旧版两路局部写互相抹黑**（已修）+ 暗光下
AGC 来回找目标造成的整幅明暗脉动（已由 LOCK_CAMERA 锁定）。待用户重看三段测试确认。

### 16.7 闪烁排查续 2：动画 vs 相机 vs 冻结（2026-10-16 深夜）

用户观察：**P1/P2/P3 三段都闪**（连最简的 P1 纯画面段也闪）→ 与检测、录像无关。
用户提问"是不是刷新率太慢"→ 面板刷新率由驱动/硬件固定 56Hz（`lcd.init` 拒绝其它值），
不可调；而合成动画能跑到 59.5fps，说明上屏速率本身不是瓶颈（低帧率只让人眼觉得动作卡，
不等于闪烁）。

关键认识：**16.5 的静态测试有缺陷** —— 它每轮 present 的都是**像素完全相同的画布**，
即使存在缓冲/撕裂问题也看不出来。所以"静态不闪"只能证明背光/驱动没坏，不能证明
"内容变化时上屏干净"。为此做了 `probe_animate_test.py`，并且**右侧面板只在开头画一次、
之后完全不重绘**（如果它也跟着闪，就实锤上屏路径）：

| 段 | 内容 | fps | 帧间隔 avg/max |
|---|---|---|---|
| A | 软件合成动画（白条每帧移动，无相机参与） | 59.5 | 16.8 / 19 ms |
| B | 实时相机画面（同 app 的合成方式） | 41.2 | 24.2 / 35 ms |
| C | 相机持续采集但画布冻结 | 41.3 | 24.2 / 26 ms |

判读：A 闪 → 内容变化时上屏有问题（present/latch/双缓冲）；只有 B 闪 → 问题在相机
画面本身（暗场景噪点/曝光）；C 闪 → 单纯 CSI 采集活动就干扰显示（带宽）。
待用户对 A/B/C 的观察结论定位。
