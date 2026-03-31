"""
plugins/examples/perception_plugin/plugin.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Perception Plugin (Example)

Runs YOLOv8 on the Go2's front camera stream and reports:
  • Human detection → behavior engine on_human_detected()
  • Obstacle detection → behavior engine on_obstacle_detected()
  • Object labels → working memory

Requires: pip install ultralytics opencv-python
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cerberus.plugins.plugin_manager import CerberusPlugin, PluginManifest, TrustLevel

logger = logging.getLogger(__name__)

MANIFEST = PluginManifest(
    name="Perception",
    version="1.0.0",
    author="CERBERUS Team",
    description="YOLOv8 object detection on Go2 front camera",
    capabilities=["read_state", "access_memory", "publish_events"],
    trust=TrustLevel.TRUSTED,
    min_cerberus="2.0.0",
)


class PerceptionPlugin(CerberusPlugin):
    MANIFEST = MANIFEST

    def __init__(self, engine):
        super().__init__(engine)
        self._model = None
        self._cap = None
        self._last_frame = None
        self._detect_interval = 3  # Run detection every N ticks (~2Hz at 60Hz engine)
        self._available = False

    async def on_load(self) -> None:
        try:
            import ultralytics
            from ultralytics import YOLO
            import cv2
            self._model = YOLO("yolov8n.pt")  # nano model — fast inference
            self._available = True
            logger.info("Perception plugin: YOLOv8 model loaded")
        except ImportError:
            logger.warning(
                "Perception plugin: ultralytics/opencv not installed. "
                "Run: pip install ultralytics opencv-python\n"
                "Plugin will run in stub mode."
            )
            self._available = False

    async def on_unload(self) -> None:
        if self._cap is not None:
            import cv2
            self._cap.release()
            self._cap = None

    async def on_tick(self, tick: int) -> None:
        if tick % self._detect_interval != 0:
            return
        if not self._available:
            return  # Stub mode — no-op

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, self._run_inference)
        if results is None:
            return

        labels, boxes, human_detected, obstacle_detected = results

        # Update behavior engine
        be = self.engine.behavior_engine
        if be is not None:
            be.on_human_detected(human_detected)
            be.on_obstacle_detected(obstacle_detected)

        # Write to working memory
        self.write_memory("detected_objects", labels, ttl_s=2.0)
        self.write_memory("human_detected", human_detected, ttl_s=2.0)
        self.write_memory("obstacle_near", obstacle_detected, ttl_s=1.5)

        # Publish events
        if human_detected:
            await self.publish("perception.human_detected", {"labels": labels})
        if obstacle_detected:
            await self.publish("perception.obstacle_detected", {"labels": labels})

    def _run_inference(self):
        """Synchronous inference — called in thread pool."""
        try:
            import cv2
            if self._cap is None or not self._cap.isOpened():
                self._cap = cv2.VideoCapture(0)

            ret, frame = self._cap.read()
            if not ret or frame is None:
                return None

            results = self._model(frame, verbose=False)
            labels = []
            human_detected = False
            obstacle_detected = False

            for r in results:
                for box in r.boxes:
                    cls_id = int(box.cls[0])
                    label = self._model.names.get(cls_id, "unknown")
                    conf = float(box.conf[0])
                    if conf < 0.45:
                        continue
                    labels.append(label)
                    if label == "person":
                        human_detected = True
                    if label in ("chair", "bench", "potted plant", "dog", "cat",
                                 "wall", "box", "suitcase", "backpack"):
                        obstacle_detected = True

            return labels, [], human_detected, obstacle_detected
        except Exception as e:
            logger.debug("Perception inference error: %s", e)
            return None

    async def on_event(self, topic: str, payload: Any) -> None:
        if topic == "engine.started":
            logger.info("Perception plugin: engine started")
