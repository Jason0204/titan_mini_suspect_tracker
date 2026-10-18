# AE3 / RA8P1 Titan Mini person-detection script -- one self-contained file.
#
# Run it straight from the OpenMV IDE, or let the PC host upload and execute it over the
# OpenMV CDC `stdin` channel. Both paths use exactly this text, so the file never has to be
# generated or rewritten by the host, and it contains no host-specific code.
#
# There are exactly two things connecting it to the host:
#   * Frames -- while the host keeps STREAM_CTRL enabled the firmware pushes the CSI frame
#     buffer to the `stream` channel on its own, so the loop only has to keep calling
#     snapshot(). image.flush() is not required for this (verified on hardware).
#   * Detections -- one compact JSON line per reported frame, written with print(), which the
#     host reads from the `stdout` channel.
#
# Changed 2026-10-13: The detector is yolov8n_coco80 (person is class 0) driven by the
# firmware's own YoloV8 post-processor. Pedestrians do not always show their face, so the
# earlier BlazeFace face-only model cannot support suspect evaluation.
#
# Why coco80 and not the smaller/faster person models on the same SD card (verified on
# hardware 2026-10-13): swift_yolo_person_nano and yolov5n6_xiao_voc20 emit ALREADY-DECODED
# boxes with a nonstandard layout ((1,2268,6) = cx,cy,w,h,obj_logit,cls_logit in ~1% units),
# which the firmware's grid-based YoloV5/YoloV8 post-processors cannot decode -- they always
# return zero detections. coco80's (1,84,756) channels-first tensor is exactly what the
# firmware YoloV8 post-processor is designed for (see the official "YOLOv8 walkthrough"
# doc), and its boxes arrive as image-space (x, y, w, h) with class 0 = person.

import csi
import ml
import time
import ujson
from ml.postprocessing.ultralytics import YoloV8

MODEL_PATH = "/yolov8n_coco80_192_u55_256.tflite"
SCORE_THRESHOLD = 0.3
PERSON_CLASS = 0  # COCO class 0
# Changed 2026-10-13: Capture and inference stay decoupled. coco80 inference costs ~33 ms,
# so DETECT_EVERY=2 keeps boxes fresh while keeping the capture loop fast.
# Changed 2026-10-14: REPORT_EVERY is 1 now. The VGA failure analysis (see docs §14) showed the
# host identity keeper needs every detection it can get: with REPORT_EVERY=2 the report rate at
# VGA was only ~3 Hz, which stretched the keeper's confirmation window long enough for the
# device's churning track IDs to outrun it. One JSON line is ~180 bytes, so even at QVGA rates
# the stdout ring buffer is nowhere near back-pressure.
DETECT_EVERY = 2
REPORT_EVERY = 1

csi0 = csi.CSI()
csi0.reset()
csi0.pixformat(csi.RGB565)
csi0.framesize(csi.QVGA)
csi0.snapshot(time=2000)  # let AWB/AGC settle before the first frame

model = ml.Model(MODEL_PATH, postprocess=YoloV8(threshold=SCORE_THRESHOLD))

clock = time.clock()
frame_index = 0
report_index = 0
frame_size = [0, 0]

# --- track-keeper block (host-testable) ---------------------------------------
# Everything between these markers is pure Python with no hardware imports, so the host can
# exec exactly this text and simulate tracker behaviour (probe_device_tracker_sim.py).
#
# Changed 2026-10-13: Device-side IoU tracker. The firmware post-processor returns raw
# per-frame detections with no identity, so this keeps IDs stable while a pedestrian stays
# in view. The host locks the suspect by track_id and never has to send anything downstream.
#
# Changed 2026-10-14: VGA halved the detection cadence, so a walking pedestrian moved further
# between two detection passes than a raw-IoU gate allows -- the tracker kept issuing NEW IDs
# (measured on hardware: 4 -> 35 -> 42 -> 52 -> 63 -> 87 -> 151 within ~70 s) and the host
# suspect lock died with every ID change. The matcher now (a) predicts each track's box with
# its exponentially smoothed centre velocity before computing IoU and (b) falls back to a
# box-size-scaled centre-distance gate when prediction and detection no longer overlap at all.
IOU_MATCH_THRESHOLD = 0.25
MAX_MISSES = 8
_next_track_id = 1
_active_tracks: list = []  # entries: {"id", "bbox", "vx", "vy", "misses"}


def _iou(first, second):
    intersection_width = min(first[2], second[2]) - max(first[0], second[0])
    intersection_height = min(first[3], second[3]) - max(first[1], second[1])
    if intersection_width <= 0 or intersection_height <= 0:
        return 0.0
    intersection = intersection_width * intersection_height
    area_first = (first[2] - first[0]) * (first[3] - first[1])
    area_second = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (area_first + area_second - intersection)


def _remember_motion(track, detection):
    # Exponentially smoothed centre displacement. A jump larger than 1.2 box sides is treated as
    # an identity re-bind and resets the velocity estimate -- everything below that is absorbed
    # so genuine brisk walking can still ACQUIRE a velocity (a strict limit would keep resetting
    # it and the prediction would never start working; verified in the host simulation).
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
    # A partial overlap alone (IoU below the match threshold) must NOT disqualify the detection:
    # a briskly walking far pedestrian grazes the predicted box with IoU ~0.1-0.2 while the
    # centre distance is well inside the gate. Rank by the better of the two scores. The
    # fallback lives in (threshold, 0.5] so it can never outrank a real box overlap (IoU -> 1).
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
    # detections: list of (x1, y1, x2, y2, score). Greedy best-score matching per track.
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
    return [
        {"id": track["id"], "bbox": track["bbox"], "misses": track["misses"]}
        for track in _active_tracks
    ]
# --- end track-keeper block ----------------------------------------------------


while True:
    clock.tick()
    image = csi0.snapshot()
    frame_index += 1
    if frame_index % DETECT_EVERY:
        continue

    # Changed 2026-10-13: The firmware YoloV8 post-processor returns one detection list per
    # class; class 0 is "person". Each entry is ([x, y, w, h], score) with x/y/w/h already in
    # input-image pixels (verified on hardware against the live stream).
    detections = []
    results = model.predict([image])
    if results:
        for entry in results[PERSON_CLASS]:
            x, y, width, height = entry[0]
            score = entry[1]
            x2 = min(image.width() - 1, int(x + width))
            y2 = min(image.height() - 1, int(y + height))
            x1 = max(0, int(x))
            y1 = max(0, int(y))
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append((x1, y1, x2, y2, float(score)))
    frame_size = [image.width(), image.height()]
    tracked = _update_tracks(detections)

    report_index += 1
    if report_index % REPORT_EVERY:
        continue

    tracks = [{
        "track_id": track["id"],
        "bbox": [round(track["bbox"][0]), round(track["bbox"][1]),
                 round(track["bbox"][2]), round(track["bbox"][3])],
        "confidence": round(track["bbox"][4], 3),
        "label": "person",
    } for track in tracked]

    print(ujson.dumps({
        "kind": "event",
        "name": "tracks_update",
        "payload": {
            "tracks": tracks,
            "frame_size": frame_size,
            "fps": round(clock.fps(), 1),
        },
    }))
