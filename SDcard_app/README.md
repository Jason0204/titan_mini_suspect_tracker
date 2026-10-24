# Udisk_app — Titan Mini SD 卡自启动文件集（归档/拷卡暂存）

本目录 = 烧写到设备 SD 卡**根目录**的全部文件，一站式暂存。
更新代码后用本目录整体覆盖 SD 卡根目录即可恢复自启动离线守卫。

## 文件清单（2026-10-24 归档）

| 文件 | 大小 | 说明 |
|---|---|---|
| `main.py` | 61,769 B | 离线守卫页（BUILD_TAG g2026-10-24a），上电自启动。= `src/device/titan_offline_guard.py`，md5 192ebe7141 与 EXE 内嵌版一致 |
| `yolov8n_coco80_192_u55_256.tflite` | 2.1 MB | YOLOv8n 行人检测模型（ulab/eNPU 布局） |
| `yolov8n_coco80_192_u55_256.txt` | 701 B | 模型标签表（COCO 80 类） |
| `openmv-vision.bin` | 1.4 MB | OpenMV 固件视觉资源，**勿删** |

## 拷卡步骤

1. 设备端让 SD 可访问：`board.usb_msc(True)`（或用上位机「导出录像/日志」流程），出盘符后拷贝。
2. 把本目录 4 个文件复制到 SD 卡**根目录**（不是子目录）。
3. `/tracker/` 子目录（boot_count、sd_capacity_gb.txt、rec、log）为运行产物，程序自建，无需预置。
4. 弹出后设备 `board.usb_msc(False)` + reset（CDC 掉线约数秒，复位等待 ~12s）。

## 注意

- SD 卡**白名单** = 上述 4 文件 + `/tracker/`；其余 log/rec 均为运行产物可清理。
- 设备侧 `os.listdir` 会截断大目录（92 段只列 3~8 个），清点卡内容需上位机 MSC 通道。
- 更换守卫页版本时，必须同步更新 EXE 内嵌脚本（`build_exe.ps1` 重打包），
  上位机连接时会用 `_MEIPASS` 里的页面文本上传覆盖，两处版本须一致。
