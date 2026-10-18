# Hardware Video Interface

## UI 内部图像格式

显示、检测框绘制和 MP4 录像统一使用：

- Python 类型：`numpy.ndarray`
- 数据类型：`uint8`
- 排列：`H x W x 3`
- 通道顺序：BGR
- 内存：C-contiguous

硬件传来的图像不需要直接满足此格式。`HardwareVideoSource` 会将协议 payload 解码成上述格式，再通过现有 `frame_ready(frame, detections)` 信号送进 UI。

## 推荐传输格式

HS USB 联调首选 **JPEG/MJPEG 单帧**：每个 payload 是一张完整 JPEG 图片。

- `1280x720 BGR24 @ 30 FPS` 原始数据约为 83 MB/s，通常超过 USB 2.0 HS 的稳定应用吞吐。
- `1280x720 YUYV422 @ 30 FPS` 约为 55 MB/s，也较紧张。
- JPEG 质量 75-90 时通常可降到每帧几十至几百 KB，适合先完成 UI 和硬件链路验证。
- 后续需要更高画质或更低带宽时，建议硬件输出 H.264 elementary stream，并在 PC 端改用 FFmpeg/PyAV 解码；不要把 H.264 NAL 当成 JPEG 帧发送。

当前支持：

| format_id | 格式 | payload | stride |
| ---: | --- | --- | ---: |
| 1 | BGR24 | 逐行 B、G、R | 每行字节数，紧密排列时为 `width * 3` |
| 2 | RGB24 | 逐行 R、G、B | 每行字节数，紧密排列时为 `width * 3` |
| 3 | JPEG | 一张完整 JPEG 文件内容 | `0` |
| 4 | YUYV422/YUY2 | `Y0 U0 Y1 V0` | 每行字节数，紧密排列时为 `width * 2` |
| 5 | NV12 | 完整 Y plane，随后交错 UV plane | 当前要求 `0` 或紧密排列 |

## 二进制帧头

所有多字节整数均为 little-endian。帧由固定 32 字节头和紧随其后的 payload 构成。`src/device/openmv_ae3_raw_jpeg.py` 文件名沿用早期裸 JPEG 原型；当前内容已改为 AE3 CSI/RGB565 采集，由 OpenMV stream 通道经 CDC 专用协议传输，不需要 `TVF1` 头，也不是裸串口 JPEG。

| Offset | Size | 字段 | 说明 |
| ---: | ---: | --- | --- |
| 0 | 4 | magic | ASCII `TVF1` |
| 4 | 1 | version | 当前为 `1` |
| 5 | 1 | format_id | 见上表 |
| 6 | 2 | header_size | 当前为 `32` |
| 8 | 4 | payload_size | payload 字节数，最大 32 MiB |
| 12 | 2 | width | 图像宽度 |
| 14 | 2 | height | 图像高度 |
| 16 | 4 | stride | 原始图每行字节数；JPEG 填 `0` |
| 20 | 4 | frame_id | 递增帧号，允许回绕 |
| 24 | 8 | timestamp_us | 采集时间戳，单位微秒 |

C 端结构体示例：

```c
#pragma pack(push, 1)
typedef struct {
    char magic[4];
    uint8_t version;
    uint8_t format_id;
    uint16_t header_size;
    uint32_t payload_size;
    uint16_t width;
    uint16_t height;
    uint32_t stride;
    uint32_t frame_id;
    uint64_t timestamp_us;
} TitanVideoFrameHeader;
#pragma pack(pop)

_Static_assert(sizeof(TitanVideoFrameHeader) == 32, "Unexpected frame header size");
```

发送时先发送整个 header，再发送 `payload_size` 字节 payload。一次帧可以跨多个 USB transfer；PC 端 `FramePacketAssembler` 会自动重组并从噪声或丢包后重新寻找 `TVF1`。

## PC 端接入

```python
from suspect_tracker.hardware_video import HardwareVideoSource
from suspect_tracker.ui.main_window import MainWindow

source = HardwareVideoSource(width=1280, height=720, fps=30)
window = MainWindow(video_source=source)

# USB 读取线程每获得一块 bytes 就调用一次；不要求 chunk 是完整帧。
source.submit_usb_chunk(chunk)
```

## OpenMV AE3 USB 串口

OpenMV AE3 的 USB CDC 使用 OpenMV 专用相机协议，不是普通 MicroPython Raw REPL，也不会直接作为 UVC 摄像头出现。UI 连接 AE3 时会同步 OpenMV 协议、枚举通道、停止旧脚本，将 CSI 采集脚本写入 `stdin` 通道并执行，然后从 `stream` 通道读取视频帧；不会改写设备文件系统或覆盖 `main.py`。

连接步骤：

1. 用数据线连接 AE3，并关闭 OpenMV IDE，确保 COM 口可被 UI 独占打开。
2. 在 UI 的 `AE3 串口` 中选择设备管理器出现的 `COM` 口；如果列表未更新，可直接在可编辑端口框输入，例如 `COM4`。
3. 点击“连接”。UI 会同步 OpenMV CDC 协议、停止设备上当前脚本，并运行打包的 `src/device/openmv_ae3_raw_jpeg.py`（AE3 CSI/RGB565 采集程序）。
4. 连接后 UI 自动启动 stream 通道并显示视频；停止视频流会暂停帧读取，断开时会关闭 stream 并停止相机脚本。

设备脚本还会注册 `tracker` 自定义 channel。UI 通过该通道发送 JSON Lines 控制命令，设备端可从同一通道回传追踪事件；AE3 人脸检测模型和检测事件目前尚未接入，因此当前脚本只接收嫌疑人选择/清除信息，不产生识别框。USB CDC 的波特率参数仅为串口驱动兼容参数，实际链路为 USB。设备脚本通过 OpenMV CDC 的 `stdin` 通道执行，不依赖 Raw REPL。

示例脚本使用 AE3 的 `csi.CSI()` 采集 API；PC 端从 OpenMV `stream` 通道取得帧数据，并转换 RGB565、JPEG 或灰阶格式。

## Titan Mini Board 控制面设计

Titan Mini Board 的控制面尚未接入 AE3 的 OpenMV CDC 协议。若后续通过通用 USB transport 接入，可使用两个独立通道：

- 视频 IN endpoint：硬件到 PC，发送上述 `TVF1` 二进制帧。
- 控制通道：USB CDC 或一对 Bulk OUT/IN endpoint，使用 UTF-8 JSON Lines，一行一个命令或事件。

通用控制命令示例（不用于当前 AE3 CDC 连接）：

```json
{"kind":"command","name":"start_stream","payload":{"width":1280,"height":720,"fps":30}}
{"kind":"command","name":"stop_stream","payload":{}}
{"kind":"command","name":"mark_suspect","payload":{"track_id":12,"label":"suspect-A","roi":[420,160,610,430],"normalized_roi":[0.328125,0.222222,0.476563,0.597222],"frame_size":[1280,720],"selection_mode":"manual_roi"}}
{"kind":"command","name":"clear_suspect","payload":{"track_id":12}}
```

`roi` 坐标格式固定为 `[x1, y1, x2, y2]`，左上角包含，右下角不包含。`track_id` 允许为 `null`：表示 UI 框选区域未匹配到上位机已知目标，硬件应在 ROI 内做人脸/目标匹配并建立追踪 ID。归一化坐标范围为 `0.0-1.0`，用于分辨率发生变化时复用选区。

硬件发给上位机的多人检测事件：

```json
{"kind":"event","name":"tracks_update","payload":{"frame_id":1038,"tracks":[{"track_id":12,"bbox":[420,160,610,430],"confidence":0.94,"label":"person"},{"track_id":18,"bbox":[760,140,910,405],"confidence":0.89,"label":"person"}]}}
```

当硬件确认追踪任务结束时，可以发送：

```json
{"kind":"event","name":"tracking_complete","payload":{"track_id":12,"reason":"operator_stop"}}
```

如果上位机发送的 `mark_suspect` 中 `track_id` 为 `null`，硬件在 ROI 内完成匹配后应先返回：

```json
{"kind":"event","name":"suspect_acquired","payload":{"track_id":12}}
```

随后硬件需要持续通过 `track_update` 或 `tracks_update` 上报该 `track_id` 的最新 `bbox`。UI 中暂停时的黄色虚线框只是一次性选择区域；重新启动视频后会清除。红色实线追踪框由最新 `bbox` 驱动，因此会随嫌疑人移动。若硬件只传视频像素、没有持续传目标元数据，UI 无法自行推断追踪框的新位置。

自动录像模式下，UI 在 `mark_suspect` 后开始本地录像，在收到 `tracking_complete` 或用户点击“清除标记”后停止录像。手动模式下，录像完全由操作员按钮控制。

## 操作时序

1. 上位机发送 `start_stream`，硬件开始发送检测后的视频帧和多人目标元数据。
2. 操作员发送 `stop_stream`，UI 保留最后一帧作为冻结画面。
3. 操作员在冻结画面拖拽 ROI，UI 使用检测框匹配 `track_id`。
4. UI 发送 `mark_suspect`；即使没有匹配到 `track_id`，硬件也可根据 ROI 完成目标选择。
5. 上位机再次发送 `start_stream`，硬件只需持续追踪已标记目标，同时仍可输出其他人脸检测结果。
6. 手动或自动记录轨迹视频；任务结束后发送 `tracking_complete`。
