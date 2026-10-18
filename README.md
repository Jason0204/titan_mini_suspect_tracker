# Titan Mini Board Suspect Tracker UI

基于 Python + PySide6 的嫌疑人识别和追踪系统 UI，当前通过 OpenMV AE3 USB CDC 连接相机。

**工程整体介绍（架构、协议实现、与 OpenMV IDE 的对接）见
[docs/engineering_overview.md](docs/engineering_overview.md)。**

## 目标架构

当前设备控制与视频流程：

- UI 使用 OpenMV CDC 执行 AE3 CSI 采集脚本，并通过 `stream` 通道接收图像。
- 检测结果经 `stdout` 通道以 JSON Lines 回传；嫌疑人选择/清除在 UI 本地完成（现场固件无自定义通道，下行命令已降级，见 `docs/engineering_overview.md` §3.11）。
- UI 负责画面显示、嫌疑人 ROI 选择、轨迹展示和本地录像；人脸模型推理需由 AE3 端接入模型后实现。

UI 仅提供 OpenMV AE3 硬件连接，全程使用 OpenMV 固件自带的 CDC 二进制协议（与 OpenMV IDE 同一套，详见 `docs/engineering_overview.md`）；未连接相机时的视频源仅用于空闲画面预览。

## 目录结构

```
src/
├── suspect_tracker/        上位机应用包（PYTHONPATH=src 后 python -m suspect_tracker）
├── device/                 设备端 MicroPython 脚本，连接时经 stdin 上传，不在本机执行
└── tests/                  验证脚本，直接运行，不参与打包
docs/                       工程介绍、协议说明、排查记录、diagrams/*.png 框图
build_exe.ps1               PyInstaller 打包（spec 由它自动生成，不入库）
run_ui.bat                  源码方式启动
```

## 快速运行

安装依赖后，可以直接双击根目录的 `run_ui.bat`，无需手动设置 `PYTHONPATH` 或输入命令。

命令行启动方式：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m suspect_tracker
```

## 构建 Windows EXE

在 PowerShell 中执行：

```powershell
.\build_exe.ps1
```

脚本会安装 PyInstaller 并生成单文件 `dist\TitanSuspectTracker.exe`，可以直接复制和双击运行。首次启动时会解压 Qt/OpenCV 运行库，因此通常比 `run_ui.bat` 慢几秒。

## UI 功能

- 主题选择：当前内置 `Graphite / 石墨青`、`Daylight / 日间蓝`、`VS Code Black / VS Code 黑色` 三套配色。
- 语言选择：支持中文和英文界面文本切换。
- 主流程验证：`连接` -> `启动视频流` -> 点击画面中的行人锁定嫌疑人 -> `开始录像` -> `停止录像`。
- 采集分辨率：连接前在侧栏选择 QVGA / VGA，连接时生效。
- 嫌疑人选择：**推流过程中直接点击画面里的行人**即可锁定（点击框附近也算），不需要暂停、不需要框选；
  锁定后红框跟随目标，`清除标记` 解除。
- 录像格式：使用 OpenCV 的 `mp4v` 编码写入 MP4；必须先启动视频流，确保有视频帧写入后再开始录像。
- 录像模式：手动模式由操作员控制；自动模式在锁定嫌疑人后开始，在清除标记或丢失目标后结束。
- 输出路径：日志写入 `<程序目录>\log\{ui_events,hardware,startup}_YYYY-MM-DD.log`（按天分文件），
  录像写入 `<程序目录>\recordings\`；程序目录不可写时回退到 `%LOCALAPPDATA%\TitanSuspectTracker`。

## OpenMV 控制通道

UI 原本计划把 JSON Lines 命令写入 OpenMV 自定义 `tracker` channel。**实测现场固件
（RA8P1 Titan Mini，MicroPython v1.28.0-openmv）不提供 `protocol` 模块，脚本无法注册
自定义 channel，因此该通道目前不存在。** 详见
[docs/openmv_ae3_protocol.md](docs/openmv_ae3_protocol.md) 第 10 节。

当前实际状态：

- 视频链路正常：脚本用 `csi.CSI()` 采集并 `flush()`，UI 从固件 `stream` 通道读帧。
- UI 连接不再要求 `tracker` 通道存在；缺失时只记录诊断信息并禁用嫌疑人命令，
  视频流和本地录像照常工作。
- 设备侧脚本已改为 `try: import protocol / except ImportError` 的降级写法，
  在有该模块的固件上仍会注册 `tracker` 并接收命令。

原定的命令格式（在有 `protocol` 模块的固件上有效）：

```json
{"kind":"command","name":"start_stream","payload":{"width":1280,"height":720,"fps":30}}
{"kind":"command","name":"mark_suspect","payload":{"track_id":12,"label":"suspect-A"}}
{"kind":"event","name":"track_update","payload":{"track_id":12,"bbox":[100,80,260,360],"confidence":0.91}}
```

当前 UI 命令：

- `mark_suspect`：把 UI 当前锁定的目标标记为嫌疑人。
- `clear_suspect`：解除标记。

人脸识别模型尚未接入：SD 卡上已有 `yolo_fastest_face_192_u55_256.tflite` 等量化模型，
可通过设备端 `ml.Model` / `ml.Workspace` 调用，但脚本侧推理循环和结果上报还没实现。


## Titan Mini Board 协议对接

上位机通过 USB CDC（COM 口）与板载 OpenMV runtime 通信，协议为 OpenMV 固件自带的
CDC 二进制协议：`SYNC → GET_CAPS → SET_CAPS → CHANNEL_LIST` 握手后，脚本走 `stdin`
通道（RAM 执行、不落盘），图像走 `stream` 通道，检测结果经 `stdout` 以 JSON Lines 回传。
实现细节见 `docs/engineering_overview.md` 第 3、4 章。

## 已移除的板端独立应用

2026-10-16 ~ 10-17 期间做过一版脱离 USB、直接跑在板上的独立应用（`titan_standalone_tracker.py`，
800x480 LCD + SD 卡录像），但尝试过多种方法无果。该方向已于 2026-10-18 停止，相关源码与
设备端截图都已从仓库删除。

## 常见问题

**`pip install -r requirements.txt` 报 `UnicodeDecodeError: 'gbk' codec can't decode byte ...`**

pip 用系统区域编码（中文 Windows 是 cp936/GBK）读取 requirements 文件，文件里只要有一个
非 ASCII 字节（比如中文注释），解析阶段就会直接崩，还没开始装包。本仓库的
`requirements.txt` / `requirements-build.txt` **强制保持纯 ASCII**；如果你本地改过它们，
请把注释改回英文，或在文件首行加 `# -*- coding: utf-8 -*-`。

**`build_exe.ps1` / `run_ui.bat` 报找不到 Python 环境**

两个脚本都会依次探测 `.venv`、`ui_venv`、`venv`。虚拟环境叫别的名字时，
`build_exe.ps1` 可用 `.\build_exe.ps1 -Venv myenv` 指定；`run_ui.bat` 改脚本里的候选列表即可。