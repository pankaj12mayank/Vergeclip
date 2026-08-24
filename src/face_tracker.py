"""
src/face_tracker.py
--------------------
Phase 4.3, 4.4 & 4.5 — Face detection, tracking, and speaker estimation.

Uses OpenCV's DNN FaceDetectorYN (YuNet ONNX model, ~232 KB, lightweight & local).
Automatically downloads the model to models/ if not present. No API key required.

For each frame:
  - Detects all faces + facial landmarks (eyes, nose, mouth)
  - Matches faces across frames with IoU & center distance
  - Smooths face positions over time using EWMA
  - Estimates active speaker via mouth activity / landmarks when multiple faces present
  - Interpolates missing frames smoothly

Public API:
    track_faces(video_path, out_path) -> list[FrameData]
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from src.config import PROJECT_ROOT
from src.logger import get_logger

log = get_logger(__name__)

# Model storage
MODELS_DIR = PROJECT_ROOT / "models"
YUNET_MODEL_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
YUNET_DOWNLOAD_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

# Tracking parameters
SCORE_THRESH = 0.55
NMS_THRESH = 0.30
DETECT_EVERY_N = 5               # Detect every N frames for speed; interpolate in between (5 = ~2.5× faster)
IOU_MATCH_THRESH = 0.25          # IoU threshold for same-face association
CENTER_DIST_THRESH_FRAC = 0.20   # Max center distance as fraction of frame width
SMOOTH_ALPHA = 0.25              # EWMA smoothing for face positions


@dataclass
class FaceBox:
    x: int
    y: int
    w: int
    h: int
    confidence: float = 1.0
    mouth_activity: float = 0.0  # Dynamic mouth movement proxy

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> int:
        return self.w * self.h

    def to_dict(self) -> dict:
        return {
            "x": int(self.x),
            "y": int(self.y),
            "w": int(self.w),
            "h": int(self.h),
            "confidence": round(float(self.confidence), 3),
            "cx": round(float(self.cx), 1),
            "cy": round(float(self.cy), 1),
            "mouth_activity": round(float(self.mouth_activity), 3),
        }


@dataclass
class TrackState:
    """Running EWMA-smoothed position for one face identity."""
    face_id: int
    sx: float   # smoothed x
    sy: float   # smoothed y
    sw: float   # smoothed w
    sh: float   # smoothed h
    confidence: float = 1.0
    last_seen_frame: int = 0
    miss_count: int = 0
    mouth_activity_history: list[float] = field(default_factory=list)

    def update(self, box: FaceBox, alpha: float = SMOOTH_ALPHA) -> None:
        self.sx = alpha * box.x + (1.0 - alpha) * self.sx
        self.sy = alpha * box.y + (1.0 - alpha) * self.sy
        self.sw = alpha * box.w + (1.0 - alpha) * self.sw
        self.sh = alpha * box.h + (1.0 - alpha) * self.sh
        self.confidence = alpha * box.confidence + (1.0 - alpha) * self.confidence
        self.miss_count = 0
        self.mouth_activity_history.append(box.mouth_activity)
        if len(self.mouth_activity_history) > 30:
            self.mouth_activity_history.pop(0)

    def as_box(self) -> FaceBox:
        avg_mouth = (
            sum(self.mouth_activity_history) / len(self.mouth_activity_history)
            if self.mouth_activity_history
            else 0.0
        )
        return FaceBox(
            x=int(self.sx),
            y=int(self.sy),
            w=int(self.sw),
            h=int(self.sh),
            confidence=self.confidence,
            mouth_activity=avg_mouth,
        )


@dataclass
class FrameData:
    frame_idx: int
    timestamp: float
    faces: list[FaceBox]
    primary_face: Optional[FaceBox]

    def to_dict(self) -> dict:
        return {
            "frame_idx": self.frame_idx,
            "timestamp": round(self.timestamp, 4),
            "faces": [f.to_dict() for f in self.faces],
            "primary_face": self.primary_face.to_dict() if self.primary_face else None,
        }


def _ensure_yunet_model() -> Path:
    """Download YuNet ONNX model if not already present."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if not YUNET_MODEL_PATH.exists() or YUNET_MODEL_PATH.stat().st_size < 10000:
        log.info("Downloading YuNet face detector model (~232 KB)...")
        req = urllib.request.Request(
            YUNET_DOWNLOAD_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp, YUNET_MODEL_PATH.open("wb") as f:
            f.write(resp.read())
        log.info("Saved YuNet model -> %s (%d bytes)", YUNET_MODEL_PATH, YUNET_MODEL_PATH.stat().st_size)
    return YUNET_MODEL_PATH


def _iou(a: FaceBox, b: FaceBox) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def _match_and_update_tracks(
    detected: list[FaceBox],
    tracks: dict[int, TrackState],
    frame_idx: int,
    frame_w: int,
    next_id: list[int],
) -> None:
    matched_track_ids = set()
    matched_det_indices = set()
    dist_thresh = CENTER_DIST_THRESH_FRAC * frame_w
    track_ids = list(tracks.keys())

    for det_idx, det in enumerate(detected):
        best_iou = IOU_MATCH_THRESH
        best_track_id = None

        for tid in track_ids:
            if tid in matched_track_ids:
                continue
            track = tracks[tid]
            existing = track.as_box()
            iou_val = _iou(existing, det)
            center_dist = ((existing.cx - det.cx) ** 2 + (existing.cy - det.cy) ** 2) ** 0.5

            if iou_val >= best_iou or (center_dist < dist_thresh):
                if iou_val > best_iou or (best_track_id is None and center_dist < dist_thresh):
                    best_iou = iou_val
                    best_track_id = tid

        if best_track_id is not None:
            tracks[best_track_id].update(det)
            tracks[best_track_id].last_seen_frame = frame_idx
            matched_track_ids.add(best_track_id)
            matched_det_indices.add(det_idx)
        else:
            new_id = next_id[0]
            next_id[0] += 1
            tracks[new_id] = TrackState(
                face_id=new_id,
                sx=float(det.x),
                sy=float(det.y),
                sw=float(det.w),
                sh=float(det.h),
                confidence=det.confidence,
                last_seen_frame=frame_idx,
                mouth_activity_history=[det.mouth_activity],
            )

    for tid in track_ids:
        if tid not in matched_track_ids:
            tracks[tid].miss_count += 1


def _select_primary_face(
    tracks: dict[int, TrackState],
    frame_h: int,
    frame_w: int,
    max_miss: int = 10,
) -> Optional[FaceBox]:
    active = [t for t in tracks.values() if t.miss_count <= max_miss]
    if not active:
        return None

    if len(active) == 1:
        return active[0].as_box()

    # If multiple active faces: score based on size, stability, mouth activity, and central position
    def score(t: TrackState) -> float:
        box = t.as_box()
        area_norm = (box.w * box.h) / (frame_w * frame_h)
        cx_dist = abs(box.cx - frame_w / 2.0) / frame_w
        mouth_score = box.mouth_activity * 2.0
        return area_norm * 5.0 + mouth_score - cx_dist

    best = max(active, key=score)
    return best.as_box()


def track_faces(
    video_path: Path,
    out_path: Optional[Path] = None,
) -> list[FrameData]:
    """
    Phase 4.3 & 4.4 entry point.
    Reads video_path frame-by-frame, detects faces using OpenCV YuNet DNN,
    tracks them across frames with EWMA smoothing, and writes face tracks JSON.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    model_path = _ensure_yunet_model()

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Initialize YuNet detector with video dimensions
    detector = cv2.FaceDetectorYN_create(
        str(model_path),
        "",
        (frame_w, frame_h),
        score_threshold=SCORE_THRESH,
        nms_threshold=NMS_THRESH,
        top_k=500,
    )

    log.info(
        "YuNet face tracking in %s | %dx%d @ %.2ffps | %d frames",
        video_path.name, frame_w, frame_h, fps, total_frames
    )

    tracks: dict[int, TrackState] = {}
    next_id = [0]
    frame_data: list[FrameData] = []
    prev_gray: Optional[np.ndarray] = None

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = frame_idx / fps
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        detected_faces: list[FaceBox] = []

        if frame_idx % DETECT_EVERY_N == 0:
            detector.setInputSize((frame_w, frame_h))
            _, faces_out = detector.detect(frame)

            if faces_out is not None:
                for f in faces_out:
                    x, y, w, h = int(f[0]), int(f[1]), int(f[2]), int(f[3])
                    conf = float(f[14])

                    # Calculate mouth activity if landmarks available
                    mouth_activity = 0.0
                    if len(f) >= 14 and prev_gray is not None:
                        # f[10:12] = right mouth corner, f[12:14] = left mouth corner
                        mx = int((f[10] + f[12]) / 2.0)
                        my = int((f[11] + f[13]) / 2.0)
                        mw = max(10, int(w * 0.35))
                        mh = max(10, int(h * 0.25))
                        m_x1 = max(0, mx - mw // 2)
                        m_y1 = max(0, my - mh // 2)
                        m_x2 = min(frame_w, mx + mw // 2)
                        m_y2 = min(frame_h, my + mh // 2)

                        patch_curr = gray[m_y1:m_y2, m_x1:m_x2]
                        patch_prev = prev_gray[m_y1:m_y2, m_x1:m_x2]
                        if patch_curr.shape == patch_prev.shape and patch_curr.size > 0:
                            diff = cv2.absdiff(patch_curr, patch_prev)
                            mouth_activity = float(np.mean(diff)) / 255.0

                    detected_faces.append(
                        FaceBox(
                            x=x,
                            y=y,
                            w=w,
                            h=h,
                            confidence=conf,
                            mouth_activity=mouth_activity,
                        )
                    )

            _match_and_update_tracks(detected_faces, tracks, frame_idx, frame_w, next_id)

        prev_gray = gray.copy()

        # Prune stale tracks (missed > 20 frames)
        stale = [tid for tid, t in tracks.items() if t.miss_count > 20]
        for tid in stale:
            del tracks[tid]

        primary = _select_primary_face(tracks, frame_h, frame_w)
        active_faces = [t.as_box() for t in tracks.values() if t.miss_count <= 2]

        frame_data.append(
            FrameData(
                frame_idx=frame_idx,
                timestamp=timestamp,
                faces=active_faces,
                primary_face=primary,
            )
        )
        frame_idx += 1

    cap.release()

    # Explicitly release the face detector DNN to free its memory
    try:
        detector = None  # noqa: allow re-assignment
    except Exception:
        pass
    import gc
    gc.collect()

    face_detected_frames = sum(1 for f in frame_data if f.primary_face is not None)
    coverage_pct = round(100.0 * face_detected_frames / max(len(frame_data), 1), 1)

    log.info(
        "Face tracking complete: %d/%d frames with primary face (%.1f%%)",
        face_detected_frames,
        len(frame_data),
        coverage_pct,
    )

    # Save to JSON
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "video": str(video_path),
                    "detector": "OpenCV YuNet DNN (face_detection_yunet_2023mar.onnx)",
                    "fps": fps,
                    "frame_count": len(frame_data),
                    "frame_w": frame_w,
                    "frame_h": frame_h,
                    "face_coverage_pct": coverage_pct,
                    "frames": [f.to_dict() for f in frame_data],
                },
                fh,
                ensure_ascii=False,
                indent=2,
            )
        log.info("Saved face tracks -> %s (%d frames)", out_path, len(frame_data))

    return frame_data
