"""
cerberus/perception/pipeline.py  — CERBERUS v3.1
=================================================
Perception pipeline stub.

Architecture for v4.0 (currently scaffolding):
  - Camera frame capture from Go2 front camera (WebRTC video track)
  - LIDAR pointcloud decoding (built-in to go2_webrtc_connect)
  - Object detection via YOLO v11 (optional dep: ultralytics)
  - Person detection + distance estimation
  - Semantic scene understanding

Run with `enabled: false` in cerberus.yaml until vision deps installed.

Research references:
  - M-SEVIQ dataset (Go2 stereo event cameras, 2026)
  - go2_ros2_sdk COCO object detector node
  - unitree/teleimager (ZeroMQ/WebRTC camera streaming)
  - Go2 EDU+ Jetson Orin NX 16GB / 100 TOPS onboard compute
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── Detection result types ──────────────────────────────────────────────── #

@dataclass
class Detection:
    class_name:  str
    confidence:  float
    bbox:        tuple[float, float, float, float]  # x, y, w, h (normalised)
    distance_m:  Optional[float] = None             # estimated from depth/LIDAR


@dataclass
class PerceptionFrame:
    timestamp:     float = field(default_factory=time.time)
    detections:    list[Detection] = field(default_factory=list)
    person_nearby: bool  = False
    obstacle_dist: Optional[float] = None   # metres to nearest obstacle
    raw_pointcloud: Any  = None             # numpy array when LIDAR available
    camera_frame:   Any  = None             # numpy array (BGR) when camera available


# ── Perception Pipeline ─────────────────────────────────────────────────── #

class PerceptionPipeline:
    """
    Async perception pipeline.

    Call start() to begin background processing.
    Register listeners with add_listener() to receive PerceptionFrame events.
    """

    def __init__(self, config: dict | None = None) -> None:
        self._cfg      = config or {}
        self._enabled  = self._cfg.get("enabled", False)
        self._listeners: list[Callable[[PerceptionFrame], None]] = []
        self._task:    Optional[asyncio.Task] = None
        self._last:    PerceptionFrame = PerceptionFrame()
        self._conn     = None  # WebRTC connection reference (set via attach_webrtc)

        # Optional deps
        self._yolo     = None
        self._cv2      = None

    # ── Lifecycle ──────────────────────────────────────────────────────── #

    async def start(self) -> None:
        if not self._enabled:
            logger.info("PerceptionPipeline disabled — set perception.enabled: true in config")
            return
        self._load_optional_deps()
        self._task = asyncio.create_task(self._loop())
        logger.info("PerceptionPipeline started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def attach_webrtc(self, conn: Any) -> None:
        """Attach a WebRTC connection to receive camera/LIDAR streams."""
        self._conn = conn

    # ── Listeners ──────────────────────────────────────────────────────── #

    def add_listener(self, cb: Callable[[PerceptionFrame], None]) -> None:
        self._listeners.append(cb)

    @property
    def last_frame(self) -> PerceptionFrame:
        return self._last

    # ── Internal ───────────────────────────────────────────────────────── #

    def _load_optional_deps(self) -> None:
        try:
            from ultralytics import YOLO  # type: ignore
            model_path = self._cfg.get("yolo_model", "yolo11n.pt")
            self._yolo = YOLO(model_path)
            logger.info("YOLO loaded: %s", model_path)
        except ImportError:
            logger.info("ultralytics not installed — object detection disabled")

        try:
            import cv2  # type: ignore
            self._cv2 = cv2
        except ImportError:
            pass

    async def _loop(self) -> None:
        interval = 1.0 / self._cfg.get("fps", 10.0)
        while True:
            try:
                frame = await self._process_frame()
                self._last = frame
                for cb in self._listeners:
                    try:
                        cb(frame)
                    except Exception as e:
                        logger.error("Perception listener error: %s", e)
            except Exception as e:
                logger.error("Perception loop error: %s", e)
            await asyncio.sleep(interval)

    async def _process_frame(self) -> PerceptionFrame:
        frame = PerceptionFrame()

        # ── Camera frame ───────────────────────────────────────────────── #
        if self._conn and hasattr(self._conn, "video"):
            try:
                img = await asyncio.to_thread(self._conn.video.get_frame)
                frame.camera_frame = img
            except Exception:
                pass

        # ── YOLO object detection ──────────────────────────────────────── #
        if self._yolo is not None and frame.camera_frame is not None:
            try:
                results = await asyncio.to_thread(
                    self._yolo.predict, frame.camera_frame,
                    conf=self._cfg.get("detection_confidence", 0.5),
                    verbose=False,
                )
                for r in results:
                    for box in r.boxes:
                        cls  = r.names[int(box.cls)]
                        conf = float(box.conf)
                        xywh = box.xywhn[0].tolist()
                        frame.detections.append(Detection(cls, conf, tuple(xywh)))
                        if cls == "person":
                            frame.person_nearby = True
            except Exception as e:
                logger.debug("YOLO inference error: %s", e)

        # ── LIDAR pointcloud ───────────────────────────────────────────── #
        if self._conn and hasattr(self._conn, "lidar"):
            try:
                pc = await asyncio.to_thread(self._conn.lidar.get_pointcloud)
                frame.raw_pointcloud = pc
                if pc is not None:
                    import numpy as np
                    dists = np.linalg.norm(pc[:, :3], axis=1)
                    frame.obstacle_dist = float(np.min(dists)) if len(dists) > 0 else None
            except Exception:
                pass

        return frame


# ── Convenience function ────────────────────────────────────────────────── #

def make_pipeline(config: dict | None = None) -> PerceptionPipeline:
    """Create a PerceptionPipeline from config dict (or defaults)."""
    return PerceptionPipeline(config)
