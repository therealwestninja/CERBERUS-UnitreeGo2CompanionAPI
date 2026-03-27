"""
cerberus/perception/pipeline.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Perception Pipeline

Multi-sensor fusion → semantic world understanding

Architecture:
  SensorHub         — aggregates raw sensor streams (IMU, LiDAR, camera, foot)
  ObjectTracker     — multi-object tracking with ByteTrack-style IoU matching
  SceneClassifier   — semantic scene labeling (indoor/outdoor, crowded/empty)
  HumanDetector     — person detection + proximity zone classification
  SpatialMapper     — 2D occupancy grid from LiDAR + odometry
  PerceptionPipeline — integrates all above into unified world percept

Output (PerceptFrame):
  - Detected objects with class, confidence, distance, 3D position
  - Tracked humans with zone classification
  - Scene label + occupancy map
  - Safety-relevant flags (human_in_zone, obstacle_dist, etc.)

Integration:
  - Feeds CognitiveMind (attention targets, goal triggers)
  - Feeds SafetyEnforcer (obstacle_dist, human_in_zone)
  - Updates WorldModel (object registry, zone detections)
  - Drives PersonalityEngine (novel stimuli → curiosity)

Sensor modes:
  SIM  — synthetic LiDAR + YOLO-proxy detections from SimulationEngine
  HW   — real D435i (depth + RGB) + L1 LiDAR via ROS2 topics
"""

import asyncio
import logging
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..runtime import Subsystem, TickContext, Priority, SystemEventBus

log = logging.getLogger('cerberus.perception')


# ════════════════════════════════════════════════════════════════════════════
# DATA TYPES
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Detection:
    """A single detected object from camera + depth pipeline."""
    id:          str   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    track_id:    int   = 0
    label:       str   = ''               # COCO class label
    confidence:  float = 0.0
    dist_m:      float = 0.0             # estimated distance (m)
    angle_deg:   float = 0.0             # bearing from robot heading
    bbox:        Tuple[int,int,int,int] = (0,0,0,0)  # x1,y1,x2,y2 (pixels)
    pos_body:    Tuple[float,float,float] = (0.,0.,0.)  # x,y,z in body frame (m)
    first_seen:  float = field(default_factory=time.time)
    last_seen:   float = field(default_factory=time.time)
    age_frames:  int   = 0

    @property
    def is_person(self) -> bool: return self.label == 'person'

    @property
    def is_obstacle(self) -> bool:
        return self.label not in ('person',) and self.dist_m < 1.5

    def to_dict(self) -> dict:
        return {
            'track_id': self.track_id, 'label': self.label,
            'conf': round(self.confidence, 2), 'dist_m': round(self.dist_m, 2),
            'angle_deg': round(self.angle_deg, 1), 'bbox': self.bbox,
        }


@dataclass
class HumanState:
    """Tracked human with proximity zone classification."""
    track_id:    int
    dist_m:      float
    angle_deg:   float
    zone:        str     = 'far'   # danger / caution / interact / nearby / far
    first_seen:  float   = field(default_factory=time.time)
    dwell_s:     float   = 0.0     # time in current zone

    ZONE_THRESHOLDS = {
        'danger':   0.25,
        'caution':  0.50,
        'interact': 1.00,
        'nearby':   2.00,
    }

    @classmethod
    def classify_zone(cls, dist_m: float) -> str:
        for zone, threshold in cls.ZONE_THRESHOLDS.items():
            if dist_m <= threshold:
                return zone
        return 'far'

    def to_dict(self) -> dict:
        return {
            'track_id': self.track_id, 'dist_m': round(self.dist_m, 2),
            'angle_deg': round(self.angle_deg, 1), 'zone': self.zone,
            'dwell_s': round(self.dwell_s, 1),
        }


@dataclass
class OccupancyCell:
    """Single cell in the 2D occupancy grid."""
    x:     int         # grid column
    y:     int         # grid row
    occ:   float = 0.0  # [0,1] occupancy probability
    age:   float = field(default_factory=time.monotonic)

    def decay(self, decay_rate: float = 0.001):
        self.occ = max(0.0, self.occ - decay_rate)


@dataclass
class SceneLabel:
    """Semantic scene classification."""
    type:        str   = 'unknown'   # indoor / outdoor / corridor / open_space
    crowded:     bool  = False
    bright:      bool  = True
    floor_type:  str   = 'unknown'   # carpet / hardwood / tile / grass
    confidence:  float = 0.5
    updated_at:  float = field(default_factory=time.time)


@dataclass
class PerceptFrame:
    """
    Unified perception output for one timestep.
    All subsystems consume this for decision-making.
    """
    timestamp:      float = field(default_factory=time.time)
    # Objects
    detections:     List[Detection] = field(default_factory=list)
    humans:         List[HumanState] = field(default_factory=list)
    # Safety-relevant summaries (fast path for SafetyEnforcer)
    nearest_obstacle_m:  float = float('inf')
    nearest_human_m:     float = float('inf')
    human_in_danger_zone: bool = False
    human_in_caution_zone: bool = False
    # Environment
    scene:          SceneLabel = field(default_factory=SceneLabel)
    # LiDAR
    lidar_scan:     List[float] = field(default_factory=list)  # 360 radial distances
    robot_pose:     Tuple[float,float,float] = (0., 0., 0.)   # x,y,yaw

    def to_dict(self) -> dict:
        return {
            'timestamp':    self.timestamp,
            'object_count': len(self.detections),
            'human_count':  len(self.humans),
            'nearest_obstacle_m': round(self.nearest_obstacle_m, 2),
            'nearest_human_m':    round(self.nearest_human_m, 2),
            'human_in_danger_zone': self.human_in_danger_zone,
            'human_in_caution_zone': self.human_in_caution_zone,
            'scene_type':   self.scene.type,
            'detections':   [d.to_dict() for d in self.detections[:10]],
            'humans':       [h.to_dict() for h in self.humans],
        }


# ════════════════════════════════════════════════════════════════════════════
# MULTI-OBJECT TRACKER
# ════════════════════════════════════════════════════════════════════════════

class ObjectTracker:
    """
    Simple IoU-based multi-object tracker (ByteTrack-inspired).
    Maintains object identities across frames using bounding box overlap.
    """

    IOU_THRESHOLD   = 0.3
    MAX_LOST_FRAMES = 15
    MAX_TRACKS      = 50

    def __init__(self):
        self._tracks:   Dict[int, Detection] = {}
        self._next_id   = 1
        self._lost:     Dict[int, int] = {}  # track_id → lost frame count

    def update(self, raw_detections: List[dict]) -> List[Detection]:
        """
        Update tracks with new raw detections.
        Returns list of tracked Detection objects with stable IDs.
        """
        # Convert raw dicts to Detection objects
        new_dets: List[Detection] = []
        for rd in raw_detections:
            new_dets.append(Detection(
                label      = rd.get('label', 'unknown'),
                confidence = float(rd.get('conf', rd.get('confidence', 0.5))),
                dist_m     = float(rd.get('dist_m', rd.get('distance', 2.0))),
                angle_deg  = float(rd.get('angle_deg', 0.0)),
                bbox       = tuple(rd.get('bbox', (0,0,100,100))),
            ))

        # Match to existing tracks by IoU + label
        matched:   Dict[int, Detection] = {}
        unmatched: List[Detection] = list(new_dets)

        for track_id, track in list(self._tracks.items()):
            best_iou, best_idx = 0.0, -1
            for i, det in enumerate(unmatched):
                if det.label != track.label:
                    continue
                iou = self._box_iou(track.bbox, det.bbox)
                if iou > best_iou:
                    best_iou, best_idx = iou, i

            if best_idx >= 0 and best_iou >= self.IOU_THRESHOLD:
                det = unmatched.pop(best_idx)
                det.track_id   = track_id
                det.first_seen = track.first_seen
                det.age_frames = track.age_frames + 1
                matched[track_id] = det
                self._lost.pop(track_id, None)
            else:
                # Track lost
                self._lost[track_id] = self._lost.get(track_id, 0) + 1
                if self._lost[track_id] <= self.MAX_LOST_FRAMES:
                    matched[track_id] = track  # keep stale track

        # Create new tracks for unmatched detections
        for det in unmatched:
            if len(matched) < self.MAX_TRACKS:
                det.track_id = self._next_id
                self._next_id += 1
                matched[det.track_id] = det

        # Prune dead tracks
        self._tracks = {tid: d for tid, d in matched.items()
                       if self._lost.get(tid, 0) <= self.MAX_LOST_FRAMES}

        return list(self._tracks.values())

    @staticmethod
    def _box_iou(b1: tuple, b2: tuple) -> float:
        """Intersection over Union for two bounding boxes."""
        if len(b1) < 4 or len(b2) < 4:
            return 0.0
        x1 = max(b1[0], b2[0]); y1 = max(b1[1], b2[1])
        x2 = min(b1[2], b2[2]); y2 = min(b1[3], b2[3])
        inter = max(0, x2-x1) * max(0, y2-y1)
        if inter == 0: return 0.0
        a1 = max(1, (b1[2]-b1[0]) * (b1[3]-b1[1]))
        a2 = max(1, (b2[2]-b2[0]) * (b2[3]-b2[1]))
        return inter / (a1 + a2 - inter)


# ════════════════════════════════════════════════════════════════════════════
# SPATIAL MAPPER (2D occupancy grid)
# ════════════════════════════════════════════════════════════════════════════

class SpatialMapper:
    """
    Builds a 2D occupancy grid from LiDAR scans + odometry.
    Grid: 10m × 10m, 0.1m resolution = 100×100 cells.
    Uses log-odds updates for probabilistic occupancy.
    """

    GRID_SIZE_M  = 10.0
    CELL_SIZE_M  = 0.10
    GRID_CELLS   = int(GRID_SIZE_M / CELL_SIZE_M)  # 100

    LOG_OCC_FREE   = -0.4   # log-odds when free
    LOG_OCC_HIT    = 0.9    # log-odds when occupied
    LOG_OCC_MAX    = 3.5    # saturation
    LOG_OCC_MIN    = -3.5

    def __init__(self):
        n = self.GRID_CELLS
        self._grid = [[0.0] * n for _ in range(n)]  # log-odds values
        self._robot_x = 0.0  # meters (world frame)
        self._robot_y = 0.0
        self._robot_yaw = 0.0   # radians

    def update_pose(self, x: float, y: float, yaw: float):
        self._robot_x   = x
        self._robot_y   = y
        self._robot_yaw = yaw

    def update_lidar(self, scan: List[float], angle_step: float = 4.0):
        """
        Update occupancy grid from a LiDAR scan.
        scan: list of radial distances (m), one per angle step
        angle_step: degrees between scan rays
        """
        cx = self._robot_x
        cy = self._robot_y
        yaw = self._robot_yaw

        for i, dist in enumerate(scan):
            if dist <= 0.1 or dist > 8.0: continue
            ray_angle = yaw + math.radians(i * angle_step)
            # Mark hit cell as occupied
            hx = cx + dist * math.cos(ray_angle)
            hy = cy + dist * math.sin(ray_angle)
            gx, gy = self._world_to_grid(hx, hy)
            if 0 <= gx < self.GRID_CELLS and 0 <= gy < self.GRID_CELLS:
                self._grid[gy][gx] = min(self.LOG_OCC_MAX,
                    self._grid[gy][gx] + self.LOG_OCC_HIT)
            # Mark along ray as free
            for r in range(1, int(dist / self.CELL_SIZE_M)):
                fx = cx + r * self.CELL_SIZE_M * math.cos(ray_angle)
                fy = cy + r * self.CELL_SIZE_M * math.sin(ray_angle)
                gfx, gfy = self._world_to_grid(fx, fy)
                if 0 <= gfx < self.GRID_CELLS and 0 <= gfy < self.GRID_CELLS:
                    self._grid[gfy][gfx] = max(self.LOG_OCC_MIN,
                        self._grid[gfy][gfx] + self.LOG_OCC_FREE)

    def _world_to_grid(self, x: float, y: float) -> Tuple[int, int]:
        half = self.GRID_SIZE_M / 2
        gx = int((x - self._robot_x + half) / self.CELL_SIZE_M)
        gy = int((y - self._robot_y + half) / self.CELL_SIZE_M)
        return gx, gy

    def nearest_obstacle_m(self, fov_deg: float = 60.0) -> float:
        """Return nearest obstacle in the robot's forward field of view."""
        half = self.GRID_CELLS // 2
        min_dist = float('inf')
        fov_rad = math.radians(fov_deg / 2)
        for gy in range(self.GRID_CELLS):
            for gx in range(self.GRID_CELLS):
                if self._grid[gy][gx] > 1.5:  # likely occupied
                    dx = (gx - half) * self.CELL_SIZE_M
                    dy = (gy - half) * self.CELL_SIZE_M
                    dist = math.sqrt(dx*dx + dy*dy)
                    angle = math.atan2(dy, dx)
                    if abs(angle) < fov_rad:
                        min_dist = min(min_dist, dist)
        return min_dist

    def grid_snapshot(self, downsample: int = 5) -> List[List[int]]:
        """Downsampled occupancy grid for UI visualization (0=free, 1=occ, -1=unknown)."""
        n = self.GRID_CELLS // downsample
        result = []
        for gy in range(n):
            row = []
            for gx in range(n):
                val = self._grid[gy * downsample][gx * downsample]
                row.append(1 if val > 1.0 else (-1 if abs(val) < 0.1 else 0))
            result.append(row)
        return result


# ════════════════════════════════════════════════════════════════════════════
# SCENE CLASSIFIER
# ════════════════════════════════════════════════════════════════════════════

class SceneClassifier:
    """
    Classifies the semantic scene type from detection patterns + LiDAR geometry.
    Heuristic rules (no ML required for demo robustness).
    """

    def classify(self, detections: List[Detection], lidar_scan: List[float],
                 robot_pose: Tuple[float,float,float]) -> SceneLabel:
        scene = SceneLabel()

        # Count detection types
        person_count = sum(1 for d in detections if d.is_person)
        object_count = sum(1 for d in detections if not d.is_person)

        # Crowding heuristic
        scene.crowded = person_count >= 2

        # Scene type from LiDAR geometry
        if lidar_scan:
            valid = [r for r in lidar_scan if 0.1 < r < 8.0]
            if valid:
                mean_range = sum(valid) / len(valid)
                min_range  = min(valid)
                if mean_range < 2.5 and min_range < 1.0:
                    scene.type = 'corridor'
                elif mean_range > 4.0:
                    scene.type = 'open_space'
                else:
                    scene.type = 'indoor'
            else:
                scene.type = 'unknown'
        else:
            scene.type = 'unknown'

        scene.confidence = 0.7 if lidar_scan else 0.3
        scene.updated_at = time.time()
        return scene


# ════════════════════════════════════════════════════════════════════════════
# PERCEPTION PIPELINE (subsystem)
# ════════════════════════════════════════════════════════════════════════════

class PerceptionPipeline(Subsystem):
    """
    CERBERUS Perception Pipeline — unified world understanding.

    Runs at Priority.CONTROL (~10Hz sub-rate) to process sensor data
    before the COGNITION layer's 10Hz deliberative tick.

    Consumes: raw detections, LiDAR scans (from EventBus)
    Produces: PerceptFrame → published to EventBus
    """

    name     = 'perception_pipeline'
    priority = Priority.CONTROL  # runs with control loop, feeds cognition

    def __init__(self, bus: SystemEventBus):
        self._bus        = bus
        self._tracker    = ObjectTracker()
        self._mapper     = SpatialMapper()
        self._classifier = SceneClassifier()
        self._tick_count = 0
        self._runtime    = None

        # Latest raw sensor data (updated from EventBus)
        self._raw_detections: List[dict] = []
        self._lidar_scan:     List[float] = []
        self._robot_pose:     Tuple[float,float,float] = (0., 0., 0.)

        # Latest percept frame (shared state)
        self._current_frame = PerceptFrame()

        # Platform back-reference (for SafetyEnforcer updates)
        self._platform = None

        # Subscribe to sensor data events
        bus.subscribe('detections', self._on_detections)
        bus.subscribe('lidar',      self._on_lidar)

    def _on_detections(self, event: str, data: Any):
        if isinstance(data, list):
            self._raw_detections = data
        elif isinstance(data, dict):
            self._raw_detections = data.get('detections', [])

    def _on_lidar(self, event: str, data: Any):
        if isinstance(data, dict):
            self._lidar_scan = data.get('scan', [])
            pose = data.get('robot', {})
            self._robot_pose = (
                float(pose.get('x', 0)),
                float(pose.get('y', 0)),
                float(math.radians(pose.get('yaw', 0))),
            )

    async def on_start(self, runtime):
        self._runtime  = runtime
        self._platform = runtime.platform
        log.info('PerceptionPipeline started')

    async def on_tick(self, ctx: TickContext):
        self._tick_count += 1
        # Run at ~10Hz (every 50th tick of the 500Hz control loop)
        if self._tick_count % 50 != 0:
            return

        frame = await self._process_frame()
        self._current_frame = frame

        # Update spatial mapper with latest LiDAR
        if self._lidar_scan:
            self._mapper.update_pose(*self._robot_pose)
            self._mapper.update_lidar(self._lidar_scan, angle_step=4.0)

        # Publish percept
        await self._bus.emit('perception.frame', frame.to_dict(), 'perception')

        # Update SafetyEnforcer directly with safety-critical fields
        if self._platform and hasattr(self._platform, 'safety'):
            self._platform.safety.update_perception(
                human_in_zone  = frame.human_in_danger_zone or frame.human_in_caution_zone,
                obstacle_dist  = min(frame.nearest_obstacle_m,
                                     self._mapper.nearest_obstacle_m()),
            )

    async def _process_frame(self) -> PerceptFrame:
        """Process raw sensor data into a unified PerceptFrame."""
        # Run object tracker
        tracked = self._tracker.update(self._raw_detections)

        # Separate humans from objects
        humans_raw   = [d for d in tracked if d.is_person]
        objects      = [d for d in tracked if not d.is_person]

        # Build human states with zone classification
        humans: List[HumanState] = []
        for d in humans_raw:
            zone = HumanState.classify_zone(d.dist_m)
            humans.append(HumanState(
                track_id  = d.track_id,
                dist_m    = d.dist_m,
                angle_deg = d.angle_deg,
                zone      = zone,
            ))

        # Safety summaries
        obstacle_dists = [d.dist_m for d in objects if d.dist_m > 0]
        human_dists    = [h.dist_m for h in humans if h.dist_m > 0]
        nearest_obs    = min(obstacle_dists) if obstacle_dists else float('inf')
        nearest_hum    = min(human_dists)    if human_dists    else float('inf')

        # Scene classification
        scene = self._classifier.classify(tracked, self._lidar_scan, self._robot_pose)

        return PerceptFrame(
            detections           = tracked,
            humans               = humans,
            nearest_obstacle_m   = nearest_obs,
            nearest_human_m      = nearest_hum,
            human_in_danger_zone = any(h.zone == 'danger'  for h in humans),
            human_in_caution_zone= any(h.zone == 'caution' for h in humans),
            scene                = scene,
            lidar_scan           = self._lidar_scan[:90],  # downsample for output
            robot_pose           = self._robot_pose,
        )

    @property
    def current_frame(self) -> PerceptFrame:
        return self._current_frame

    def status(self) -> dict:
        f = self._current_frame
        return {
            'name':    self.name,
            'enabled': self.enabled,
            'ticks':   self._tick_count,
            'active_tracks': len(self._tracker._tracks),
            'nearest_obstacle_m': round(f.nearest_obstacle_m, 2),
            'nearest_human_m':    round(f.nearest_human_m, 2),
            'human_in_zone':      f.human_in_danger_zone or f.human_in_caution_zone,
            'scene_type':         f.scene.type,
        }
