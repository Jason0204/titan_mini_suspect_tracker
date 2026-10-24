from __future__ import annotations

import re


LANGUAGES = {
    "zh": "中文",
    "en": "English",
}


TEXT: dict[str, dict[str, str]] = {
    "zh": {
        "window_title": "Titan Mini Board 嫌疑人识别追踪系统",
        "title": "Titan Mini Board 嫌疑人识别与追踪控制台",
        "video_idle": "视频流未启动",
        # Changed 2026-09-20: Explain the suspect-selection workflow directly beside the video.
        "selection_hint_empty": "连接硬件并启动视频流。",
        # Changed 2026-10-13: Live click-to-lock replaces the pause-then-draw workflow.
        # Changed 2026-10-14: The paused-frame drag-selection workflow was removed, so the hints
        # no longer mention drawing an area.
        "selection_hint_running": "视频接收中；点击画面中的行人即可锁定嫌疑人（点框附近也可以），无需暂停。",
        "selection_hint_stopped": "视频流已停止；点击『启动视频流』并点击画面中的行人即可锁定嫌疑人。",
        "settings": "界面设置",
        "theme": "主题",
        "language": "语言",
        "control": "控制",
        "camera_port": "AE3 串口",
        # Changed 2026-10-14: The AI-profile picker and the manual label box were removed; the
        # slot now selects the capture resolution, which is applied when the camera connects.
        "frame_size": "采集分辨率",
        # Changed 2026-10-18: The device page picker (headless capture vs the on-panel LCD page).
        "device_page": "板端页面",
        "device_page_headless": "无头采集（不占用屏幕）",
        "device_page_lcd": "LCD 页面（板端同步显示）",
        # Changed 2026-09-20: Add paused-frame selection and trajectory recording terminology.
        "recording_mode": "录像模式",
        "recording_manual": "手动",
        "recording_automatic": "自动跟随嫌疑人",
        "connect": "连接",
        "disconnect": "断开",
        "start_stream": "启动视频流",
        "stop_stream": "停止视频流",
        "clear_mark": "清除标记",
        "start_recording": "开始录像",
        "stop_recording": "停止录像",
        # Changed 2026-10-24: USB MSC export of the guard's SD files (log + rec).
        "export_files": "导出录像/日志（U盘模式）",
        "export_need_port": "请先在『AE3 串口』中选择板卡 COM 口，再导出。",
        "export_started": "开始导出板卡文件：设备将临时切换 U 盘模式，完成后自动复位。",
        "export_stage_serial": "连接板卡并停止当前页面……",
        "export_stage_msc": "已请求设备切换 U 盘模式……",
        "export_stage_mount": "等待系统识别 SD 卡……",
        "export_stage_copy": "正在拷贝文件：{done}/{total}",
        "export_stage_reset": "拷贝完成，等待设备复位……",
        "export_done": "导出完成：日志 {log} 个、录像 {rec} 个（失败 {failed}）→ {dest}",
        "export_no_port_back": "设备串口未恢复，面板就绪后请手动点『连接』。",
        "export_reconnecting": "设备已复位，正在重新连接 {port}……",
        "export_reconnect_failed": "自动重连失败：{detail}（可稍后手动点『连接』）",
        "export_failed": "导出失败：{detail}",
        "status": "状态",
        "camera_connected": "相机已连接",
        "camera_disconnected": "相机未连接",
        # Changed 2026-10-14: Show the measured capture frame rate in the status panel.
        "fps": "帧率：上位机 {host:.1f} fps ｜ 设备采集 {device:.1f} fps",
        "fps_idle": "帧率：--",
        "active_target_none": "当前嫌疑人：无",
        "active_target": "当前嫌疑人：{track_id} ({label})",
        "active_target_roi": "当前嫌疑人：ROI 标记 ({label})",
        "recording_off": "录像：关闭",
        "recording_on": "录像：{path}",
        "live_tracks": "实时目标",
        "trajectory_path": "轨迹路径",
        "event_log": "命令与事件日志",
        "track_id": "目标 ID",
        "table_label": "标签",
        "confidence": "置信度",
        "bbox": "边界框",
        "recording_error": "录像错误",
        "recording_started": "开始录像：{path}",
        "recording_saved": "录像已保存：{path}",
        "recording_empty": "没有写入视频帧，空录像文件已删除。",
        "recording_unavailable_title": "无法开始录像",
        "recording_unavailable_message": "请先启动视频流，再开始录像。",
        # Changed 2026-10-14: Tell the operator what actually finalizes the MP4, and where every
        # output file lands.
        "log_file_hint": "命令与事件日志按天保存：{path}",
        "output_directory": "输出目录：{path}",
        "open_output_directory": "打开输出目录",
        "open_output_directory_failed": "无法打开输出目录：{path}",
        "automatic_recording_hint": "自动跟随录像已开始；点击『清除标记』或关闭程序即保存 MP4。",
        "automatic_recording_saved": "嫌疑人已连续丢失 {seconds:.0f} 秒，自动结束并保存录像：{path}",
        "recording_saved_on_exit": "程序退出，已保存录像：{path}",
        # Changed 2026-10-14: Log lines follow the selected language (previously English only).
        "log_camera_connected": "OpenMV 相机已连接：{port}（{frame_size} {width}x{height}，页面 {page}）",
        # Changed 2026-10-18e: the LCD page re-uploads when the host language changes mid-session.
        "log_page_language_synced": "板端页面已同步为{language}显示（视频短暂中断后自动恢复）",
        "log_page_language_failed": "板端页面语言同步失败：{detail}",
        # Changed 2026-10-18h: 守卫 -> 监控 (panel wording follows, see device/titan_offline_guard.py)
        "log_offline_guard_started": "已断开，板端已切换到离线监控：自动检测并录像，日志与录像写入 SD 卡 /tracker/",
        "log_offline_guard_failed": "已断开，但离线监控未能启动：{detail}",
        "language_zh": "中文",
        "language_en": "英文",
        "log_connection_failed": "连接失败：{detail}",
        "log_command_not_sent": "命令未发送，相机未连接：{name}",
        "log_command_not_delivered": "命令未能送达：{detail}",
        "log_command_failed": "命令执行失败：{name}：{detail}",
        "log_tx": "→ 发送 {kind}:{name}",
        "log_rx": "← 接收 {kind}:{name}",
        "log_device_info": "设备：{detail}",
        "log_device_error": "设备异常：{detail}",
        "log_video_error": "视频帧错误：{detail}",
        # Translated forms of the diagnostic messages emitted by the OpenMV driver layer.
        "diag_connecting": "正在通过 OpenMV CDC 协议连接相机…",
        "diag_connected": "OpenMV 协议已连接（{port}），采集脚本运行中。",
        "diag_no_control_channel": "该固件没有 “tracker” 控制通道：视频正常，但嫌疑人命令无法下发。",
        "diag_channel_lost": "OpenMV 控制通道 {channel} 不可用（{detail}）；视频继续，设备事件停用。",
        "diag_read_failed": "OpenMV 取帧失败（{attempt}/{maximum}）：{detail}",
        "diag_stream_reassert": "相机无帧输出，正在重新建立视频流。",
        "diag_script_reupload": "仍无帧输出，正在重新上传采集脚本。",
        "diag_recovery_failed": "OpenMV 恢复失败：{detail}",
        "diag_first_frame": "已解码首帧：{width}x{height}。",
        "diag_watchdog_gave_up": "OpenMV 已连接但相机不产帧；请检查采集脚本与 CSI 摄像头模块。",
        # Changed 2026-10-13: Live click-to-lock and suspect loss notifications.
        "suspect_picked": "已锁定嫌疑人 ID {track_id}（{label}），上位机将持续跟踪并记录轨迹。",
        "suspect_lost": "嫌疑人 ID {track_id} 暂时离开画面，保持锁定等待重新出现；如长时间未恢复请重新点选。",
        "suspect_reacquired": "嫌疑人 ID {track_id} 已重新锁定。",
        # Changed 2026-10-14: The on-device tracker can hand out a new ID after occlusion, a fast
        # move, or when two pedestrians cross; the host confirms the new ID with motion and
        # appearance before re-binding, so the red box cannot jump onto a neighbour.
        "suspect_relocked": "嫌疑人目标 ID 由 {old_id} 变更为 {new_id}（身份已重新确认），锁定保持。",
        "suspect_ambiguous": "画面中有多个相近目标，暂不移动红色选框；确认后再跟随（锁定保持）。",
        # Changed 2026-10-14: The program refuses to open a second window (one board, one COM
        # port); a duplicate launch instead brings this window to the front.
        "duplicate_launch_blocked": "检测到重复启动：程序只允许运行一个实例，已将当前窗口置于前台。",
    },
    "en": {
        "window_title": "Titan Mini Board Suspect Tracker",
        "title": "Titan Mini Board Suspect Identification & Tracking Console",
        "video_idle": "Video stream idle",
        # Changed 2026-09-20: Explain the suspect-selection workflow directly beside the video.
        "selection_hint_empty": "Connect the hardware and start the video stream.",
        # Changed 2026-10-13: Live click-to-lock replaces the pause-then-draw workflow.
        # Changed 2026-10-14: The paused-frame drag-selection workflow was removed, so the hints
        # no longer mention drawing an area.
        "selection_hint_running": "Streaming; click a pedestrian to lock the suspect (clicking near the box works too), no pause needed.",
        "selection_hint_stopped": "Stream stopped; start the stream and click a pedestrian to lock the suspect.",
        "settings": "Interface Settings",
        "theme": "Theme",
        "language": "Language",
        "control": "Control",
        "camera_port": "AE3 Serial Port",
        # Changed 2026-10-14: The AI-profile picker and the manual label box were removed; the
        # slot now selects the capture resolution, which is applied when the camera connects.
        "frame_size": "Capture Resolution",
        # Changed 2026-10-18: The device page picker (headless capture vs the on-panel LCD page).
        "device_page": "Device Page",
        "device_page_headless": "Headless capture (panel free)",
        "device_page_lcd": "LCD page (mirrored on panel)",
        # Changed 2026-09-20: Add paused-frame selection and trajectory recording terminology.
        "recording_mode": "Recording Mode",
        "recording_manual": "Manual",
        "recording_automatic": "Automatic Suspect Tracking",
        "connect": "Connect",
        "disconnect": "Disconnect",
        "start_stream": "Start Stream",
        "stop_stream": "Stop Stream",
        "clear_mark": "Clear Mark",
        "start_recording": "Start Recording",
        "stop_recording": "Stop Recording",
        # Changed 2026-10-24: USB MSC export of the guard's SD files (log + rec).
        "export_files": "Export Files (USB drive mode)",
        "export_need_port": "Select the board's COM port under 'AE3 Serial' before exporting.",
        "export_started": "Export started: the device switches to USB drive mode and resets itself afterwards.",
        "export_stage_serial": "Connecting and stopping the current page ...",
        "export_stage_msc": "USB drive mode requested ...",
        "export_stage_mount": "Waiting for the system to mount the SD card ...",
        "export_stage_copy": "Copying files: {done}/{total}",
        "export_stage_reset": "Copy done, waiting for the device to reset ...",
        "export_done": "Export finished: {log} logs, {rec} recordings ({failed} failed) -> {dest}",
        "export_no_port_back": "Serial port did not return; press 'Connect' once the panel is ready.",
        "export_reconnecting": "Device reset, reconnecting to {port} ...",
        "export_reconnect_failed": "Reconnect failed: {detail} (press 'Connect' to retry)",
        "export_failed": "Export failed: {detail}",
        "status": "Status",
        "camera_connected": "Camera connected",
        "camera_disconnected": "Camera disconnected",
        # Changed 2026-10-14: Show the measured capture frame rate in the status panel.
        "fps": "Frame rate: host {host:.1f} fps | device capture {device:.1f} fps",
        "fps_idle": "Frame rate: --",
        "active_target_none": "Active target: none",
        "active_target": "Active target: {track_id} ({label})",
        "active_target_roi": "Active target: ROI mark ({label})",
        "recording_off": "Recording: off",
        "recording_on": "Recording: {path}",
        "live_tracks": "Live Tracks",
        "trajectory_path": "Trajectory Path",
        "event_log": "Command & Event Log",
        "track_id": "Track ID",
        "table_label": "Label",
        "confidence": "Confidence",
        "bbox": "BBox",
        "recording_error": "Recording error",
        "recording_started": "Recording started: {path}",
        "recording_saved": "Recording saved: {path}",
        "recording_empty": "No video frames were written; the empty recording was removed.",
        "recording_unavailable_title": "Recording unavailable",
        "recording_unavailable_message": "Start the video stream before recording.",
        # Changed 2026-10-14: Tell the operator what actually finalizes the MP4, and where every
        # output file lands.
        "log_file_hint": "Command and event logs are stored per day: {path}",
        "output_directory": "Output directory: {path}",
        "open_output_directory": "Open Output Folder",
        "open_output_directory_failed": "Unable to open the output directory: {path}",
        "automatic_recording_hint": "Automatic follow recording started; the MP4 is saved when you clear the mark or close the program.",
        "automatic_recording_saved": "Suspect lost for {seconds:.0f} s; recording stopped and saved: {path}",
        "recording_saved_on_exit": "Program is closing; recording saved: {path}",
        # Changed 2026-10-14: Log lines follow the selected language (previously English only).
        "log_camera_connected": "OpenMV camera connected: {port} ({frame_size} {width}x{height}, page {page})",
        # Changed 2026-10-18e: the LCD page re-uploads when the host language changes mid-session.
        "log_page_language_synced": "Device page switched to {language} (video recovers after a short gap)",
        "log_page_language_failed": "Device page language sync failed: {detail}",
        "log_offline_guard_started": "Disconnected; the board switched to the offline monitor: it detects and records on its own, logs and recordings go to /tracker/ on the SD card",
        "log_offline_guard_failed": "Disconnected, but the offline monitor failed to start: {detail}",
        "language_zh": "Chinese",
        "language_en": "English",
        "log_connection_failed": "Connection failed: {detail}",
        "log_command_not_sent": "Command not sent; camera is disconnected: {name}",
        "log_command_not_delivered": "Command not delivered: {detail}",
        "log_command_failed": "Command failed: {name}: {detail}",
        "log_tx": "-> send {kind}:{name}",
        "log_rx": "<- recv {kind}:{name}",
        "log_device_info": "Device: {detail}",
        "log_device_error": "Device error: {detail}",
        "log_video_error": "Video frame error: {detail}",
        # Translated forms of the diagnostic messages emitted by the OpenMV driver layer.
        "diag_connecting": "Connecting with the OpenMV camera CDC protocol...",
        "diag_connected": "OpenMV protocol connected on {port}; camera script is running.",
        "diag_no_control_channel": "This firmware has no 'tracker' control channel: video works, suspect commands cannot be delivered.",
        "diag_channel_lost": "OpenMV control channel {channel} is unavailable ({detail}); video continues without hardware events.",
        "diag_read_failed": "OpenMV frame read failed ({attempt}/{maximum}): {detail}",
        "diag_stream_reassert": "No frames from the camera; re-asserting the video stream.",
        "diag_script_reupload": "Still no frames; re-uploading the capture script.",
        "diag_recovery_failed": "OpenMV recovery failed: {detail}",
        "diag_first_frame": "First frame decoded: {width}x{height}.",
        "diag_watchdog_gave_up": "OpenMV is connected but the camera produced no frames; check the capture script and the CSI camera module.",
        # Changed 2026-10-13: Live click-to-lock and suspect loss notifications.
        "suspect_picked": "Suspect locked: ID {track_id} ({label}); the console now tracks and records it.",
        "suspect_lost": "Suspect ID {track_id} left the view; lock kept while waiting. Re-pick if it does not return.",
        "suspect_reacquired": "Suspect ID {track_id} reacquired.",
        # Changed 2026-10-14: The on-device tracker can hand out a new ID after occlusion, a fast
        # move, or when two pedestrians cross; the host confirms the new ID with motion and
        # appearance before re-binding, so the red box cannot jump onto a neighbour.
        "suspect_relocked": "Suspect ID changed from {old_id} to {new_id} (identity re-confirmed); lock kept.",
        "suspect_ambiguous": "Several similar targets in view; the red box stays put until the match is confirmed (lock kept).",
        # Changed 2026-10-14: The program refuses to open a second window (one board, one COM
        # port); a duplicate launch instead brings this window to the front.
        "duplicate_launch_blocked": "Duplicate launch detected: only one instance may run, so this window was brought to the front.",
    },
}


def text(language: str, key: str, **values: object) -> str:
    template = TEXT.get(language, TEXT["en"]).get(key, key)
    return template.format(**values)


# Changed 2026-10-14: The OpenMV driver layer emits its diagnostics in English (it also writes
# them to the protocol trace). The UI re-renders the known messages in the selected language so
# the on-screen log matches the interface language. Unknown messages pass through unchanged.
_DIAGNOSTIC_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^Connecting with the OpenMV camera CDC protocol\.$"),
        "diag_connecting",
    ),
    (
        re.compile(r"^OpenMV protocol connected on (?P<port>.+); camera script is running\.$"),
        "diag_connected",
    ),
    (
        re.compile(
            r"^Firmware exposes no 'tracker' control channel; "
            r"video works but suspect commands cannot be delivered\.$"
        ),
        "diag_no_control_channel",
    ),
    (
        re.compile(
            r"^OpenMV control channel (?P<channel>'[^']*'|[^ ]+) is unavailable "
            r"\((?P<detail>.*)\); video continues without hardware events\.$"
        ),
        "diag_channel_lost",
    ),
    (
        re.compile(
            r"^OpenMV frame read failed \((?P<attempt>\d+)/(?P<maximum>\d+)\): (?P<detail>.+)$"
        ),
        "diag_read_failed",
    ),
    (
        re.compile(r"^No frames from the camera; re-asserting the video stream\.$"),
        "diag_stream_reassert",
    ),
    (
        re.compile(r"^Still no frames; re-uploading the AE3 capture script\.$"),
        "diag_script_reupload",
    ),
    (
        re.compile(r"^OpenMV recovery attempt failed: (?P<detail>.+)$"),
        "diag_recovery_failed",
    ),
    (
        re.compile(r"^First (?:OpenMV protocol|TVF1) frame decoded: (?P<width>\d+)x(?P<height>\d+)\.$"),
        "diag_first_frame",
    ),
    (
        re.compile(
            r"^OpenMV is connected but the camera produced no frames; "
            r"check the AE3 capture script and the CSI camera module\.$"
        ),
        "diag_watchdog_gave_up",
    ),
)


def diagnostic_text(language: str, message: str) -> str:
    if language == "en":
        return message
    for pattern, key in _DIAGNOSTIC_RULES:
        match = pattern.match(message)
        if match:
            return text(language, key, **match.groupdict())
    return message
