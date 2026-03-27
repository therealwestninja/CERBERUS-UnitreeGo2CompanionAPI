"""
go2_platform/backend/animation/animation_system.py
══════════════════════════════════════════════════════════════════════════════
Animation System — Multi-format, blending, state-driven, real-time.

Architecture:
  AnimationLoader   — format detection + parsing (FunScript, BVH, JSON, CSV)
  AnimationClip     — normalized keyframe data + metadata
  AnimationBlender  — smooth transitions between clips (linear/cubic)
  AnimationPlayer   — real-time playback engine (200Hz compatible)
  AnimationStateMachine — state-driven clip selection with priorities
  AnimationRegistry — named clip library with lazy loading + caching

Supported formats:
  .funscript  — OpenFunscripter format (pos 0-100, timestamps ms)
  .bvh        — BioVision Hierarchy motion capture (mapped to Go2 joints)
  .json       — Go2 native keyframe format (full joint positions)
  .csv        — Simple CSV: time_ms, joint_0, joint_1, ... joint_11

Integration:
  - AnimationPlayer feeds directly into MotionController
  - AnimationStateMachine subscribes to FSM events (EventBus)
  - Clips can trigger FSM transitions (e.g., 'sit' clip → SIT state)
  - Priority system: safety_override > mission > behavior > idle
"""

import asyncio
import csv
import io
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger('go2.animation')

# Joint indices matching Unitree Go2 SDK2 LowCmd
JOINT_NAMES = [
    'FR_0', 'FR_1', 'FR_2',   # Front-Right: abduction, hip, knee
    'FL_0', 'FL_1', 'FL_2',   # Front-Left
    'RR_0', 'RR_1', 'RR_2',   # Rear-Right
    'RL_0', 'RL_1', 'RL_2',   # Rear-Left
]
N_JOINTS = len(JOINT_NAMES)

# Neutral standing pose (rad) — reference for idle/breath animations
NEUTRAL_POSE = [
    0.0,  0.67, -1.3,   # FR
    0.0,  0.67, -1.3,   # FL
    0.0,  0.67, -1.3,   # RR
    0.0,  0.67, -1.3,   # RL
]


class AnimPriority(Enum):
    """Higher value = higher priority; overrides lower priority clips."""
    IDLE       = 0
    BACKGROUND = 1
    BEHAVIOR   = 2
    MISSION    = 3
    REACTION   = 4
    OVERRIDE   = 5


class AnimState(Enum):
    IDLE     = auto()
    LOADING  = auto()
    READY    = auto()
    PLAYING  = auto()
    PAUSED   = auto()
    BLENDING = auto()
    ERROR    = auto()


class BlendMode(Enum):
    LINEAR   = 'linear'
    CUBIC    = 'cubic'
    ADDITIVE = 'additive'   # add offsets to base pose


# ════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Keyframe:
    """Single animation keyframe — time + all 12 joint positions."""
    time_ms: float
    joints: List[float]      # length N_JOINTS (radians)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def interpolate_to(self, other: 'Keyframe', t: float,
                       mode: BlendMode = BlendMode.CUBIC) -> 'Keyframe':
        """
        Interpolate between this keyframe and `other` at parameter t ∈ [0,1].
        Cubic (Hermite) blending produces smoother motion than linear.
        """
        if mode == BlendMode.LINEAR:
            joints = [a + (b - a) * t for a, b in zip(self.joints, other.joints)]
        else:
            # Cubic Hermite: smoothstep
            ts = t * t * (3 - 2 * t)
            joints = [a + (b - a) * ts for a, b in zip(self.joints, other.joints)]
        return Keyframe(
            time_ms=self.time_ms + (other.time_ms - self.time_ms) * t,
            joints=joints,
        )


@dataclass
class AnimationClip:
    """
    Normalized animation clip — format-agnostic internal representation.
    All clips are stored as sequences of Keyframes with joint positions in radians.
    """
    id:           str
    name:         str
    source_format: str         # 'funscript' | 'bvh' | 'json' | 'csv' | 'procedural'
    duration_ms:  float
    keyframes:    List[Keyframe]
    loop:         bool = False
    priority:     AnimPriority = AnimPriority.BEHAVIOR
    blend_in_ms:  float = 200.0    # time to blend into this clip
    blend_out_ms: float = 200.0    # time to blend out
    tags:         List[str] = field(default_factory=list)
    triggers:     Dict[str, Any] = field(default_factory=dict)  # FSM/event triggers
    metadata:     Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.keyframes:
            raise ValueError(f'Clip {self.id!r}: no keyframes')
        # Ensure sorted by time
        self.keyframes.sort(key=lambda k: k.time_ms)

    def sample(self, time_ms: float) -> Keyframe:
        """
        Sample clip at given time using binary search + interpolation.
        Handles loop wrapping and out-of-bounds clamping.
        """
        if self.loop and self.duration_ms > 0:
            time_ms = time_ms % self.duration_ms

        kfs = self.keyframes
        # Clamp to valid range
        time_ms = max(kfs[0].time_ms, min(kfs[-1].time_ms, time_ms))

        # Binary search for surrounding keyframes
        lo, hi = 0, len(kfs) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if kfs[mid].time_ms <= time_ms:
                lo = mid
            else:
                hi = mid

        if kfs[lo].time_ms == kfs[hi].time_ms:
            return kfs[lo]

        t = (time_ms - kfs[lo].time_ms) / (kfs[hi].time_ms - kfs[lo].time_ms)
        return kfs[lo].interpolate_to(kfs[hi], t)

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'name': self.name,
            'source_format': self.source_format,
            'duration_ms': self.duration_ms,
            'keyframe_count': len(self.keyframes),
            'loop': self.loop,
            'priority': self.priority.name,
            'blend_in_ms': self.blend_in_ms,
            'blend_out_ms': self.blend_out_ms,
            'tags': self.tags,
        }


# ════════════════════════════════════════════════════════════════════════════
# FORMAT LOADERS
# ════════════════════════════════════════════════════════════════════════════

class AnimationLoader:
    """
    Multi-format animation loader.
    Auto-detects format from file extension or content structure.
    All formats normalized to AnimationClip.
    """

    @classmethod
    def load(cls, data: Any, fmt: Optional[str] = None,
             clip_id: str = 'clip', name: str = 'Unnamed') -> AnimationClip:
        """
        Load animation from various formats.
        `data` can be: str (file path), bytes, dict (JSON), or str content.
        """
        if fmt is None:
            fmt = cls._detect_format(data)
        loaders = {
            'funscript': cls._load_funscript,
            'bvh':       cls._load_bvh,
            'json':      cls._load_json,
            'csv':       cls._load_csv,
        }
        loader = loaders.get(fmt)
        if not loader:
            raise ValueError(f'Unsupported animation format: {fmt!r}')
        return loader(data, clip_id=clip_id, name=name)

    @staticmethod
    def _detect_format(data: Any) -> str:
        if isinstance(data, dict):
            if 'actions' in data or 'rawActions' in data:
                return 'funscript'
            if 'keyframes' in data or 'joints' in data:
                return 'json'
        if isinstance(data, str):
            if data.strip().startswith('HIERARCHY'):
                return 'bvh'
            if data.strip().startswith('{'):
                return 'json'
            if ',' in data.split('\n')[0]:
                return 'csv'
        return 'json'

    @classmethod
    def _load_funscript(cls, data: Any, clip_id: str, name: str) -> AnimationClip:
        """
        Load FunScript format.
        pos [0-100] is mapped to a blend of standing and a target pose.
        Velocity-derived rhythm maps to oscillation speed.

        FunScript spec:
          {"version":"1.0","range":90,"actions":[{"at":ms,"pos":0-100},...]}
        """
        if isinstance(data, (str, bytes)):
            data = json.loads(data)

        raw_acts = data.get('actions') or data.get('rawActions') or []
        if not raw_acts:
            raise ValueError('FunScript: no actions array found')

        acts = sorted(
            [{'at': float(a['at']),
              'pos': max(0.0, min(100.0, float(a.get('pos', a.get('position', 0)))))}
             for a in raw_acts],
            key=lambda x: x['at']
        )
        fs_range = float(data.get('range', 90))
        duration_ms = acts[-1]['at']

        keyframes = []
        for a in acts:
            # Map FunScript pos [0-100] to joint pose blend
            # 0 = neutral standing, 100 = maximum rear-leg extension
            t_norm = a['pos'] / 100.0
            joints = _blend_poses(NEUTRAL_POSE, _ENGAGE_POSE, t_norm * (fs_range / 90))
            keyframes.append(Keyframe(time_ms=a['at'], joints=joints,
                                      metadata={'pos': a['pos']}))

        meta = data.get('metadata', {})
        return AnimationClip(
            id=clip_id, name=meta.get('title', name),
            source_format='funscript', duration_ms=duration_ms,
            keyframes=keyframes, loop=data.get('loop', False),
            metadata={'funscript_range': fs_range,
                      'inverted': data.get('inverted', False),
                      'creator': meta.get('creator', ''),
                      'original_actions': len(raw_acts)},
        )

    @classmethod
    def _load_bvh(cls, data: str, clip_id: str, name: str) -> AnimationClip:
        """
        Load BVH (BioVision Hierarchy) motion capture format.
        Maps BVH joint channels to Go2 joint indices heuristically.

        BVH structure:
          HIERARCHY block → joint tree definition
          MOTION block → frame data (Nframes * Nchannels floats)

        Mapping: Right hip → FR_1, Left hip → FL_1,
                 Right knee → FR_2, Left knee → FL_2, etc.
        """
        lines = data.strip().split('\n')
        # Parse MOTION block
        motion_idx = next((i for i, l in enumerate(lines)
                           if l.strip() == 'MOTION'), None)
        if motion_idx is None:
            raise ValueError('BVH: no MOTION block found')

        # Extract frame count and frame time
        frames_line = lines[motion_idx + 1]
        frame_time_line = lines[motion_idx + 2]
        try:
            n_frames = int(frames_line.split(':')[1].strip())
            frame_time_s = float(frame_time_line.split(':')[1].strip())
        except (IndexError, ValueError) as e:
            raise ValueError(f'BVH: malformed MOTION header: {e}') from e

        frame_time_ms = frame_time_s * 1000.0
        keyframes = []

        for i in range(n_frames):
            line_idx = motion_idx + 3 + i
            if line_idx >= len(lines):
                break
            try:
                values = [float(v) for v in lines[line_idx].split()]
            except ValueError:
                continue

            # Map BVH channels → Go2 joints (simplified heuristic mapping)
            # In practice, this requires parsing the HIERARCHY to get exact channel layout
            joints = list(NEUTRAL_POSE)  # start from neutral

            # Apply rotation channels as offsets (deg → rad, clamped)
            if len(values) >= 6:
                # Assume first 6 values are root position/rotation (ignore translation)
                # Next values are joint rotations in order of HIERARCHY
                n_channels = len(values)
                n_extra = n_channels - 6
                stride = max(1, n_extra // N_JOINTS)
                for ji in range(min(N_JOINTS, n_extra // stride)):
                    channel_val = values[6 + ji * stride] if (6 + ji * stride) < len(values) else 0
                    offset_rad = math.radians(channel_val) * 0.3  # scale down BVH rotations
                    joints[ji] = max(-2.5, min(2.5, NEUTRAL_POSE[ji] + offset_rad))

            keyframes.append(Keyframe(time_ms=i * frame_time_ms, joints=joints))

        if not keyframes:
            raise ValueError('BVH: no valid frame data extracted')

        duration_ms = (n_frames - 1) * frame_time_ms
        return AnimationClip(
            id=clip_id, name=name, source_format='bvh',
            duration_ms=duration_ms, keyframes=keyframes,
            metadata={'n_frames': n_frames, 'frame_time_ms': frame_time_ms},
        )

    @classmethod
    def _load_json(cls, data: Any, clip_id: str, name: str) -> AnimationClip:
        """
        Load Go2 native JSON keyframe format.
        Schema:
        {
          "name": "sit_animation",
          "loop": false,
          "blend_in_ms": 200,
          "keyframes": [
            {"time_ms": 0, "joints": [0,0.67,-1.3, 0,0.67,-1.3, 0,0.67,-1.3, 0,0.67,-1.3]},
            {"time_ms": 500, "joints": [0,0.8,-1.5, 0,0.8,-1.5, 0,1.5,-2.4, 0,1.5,-2.4]},
            ...
          ]
        }
        """
        if isinstance(data, (str, bytes)):
            data = json.loads(data)

        raw_kfs = data.get('keyframes', [])
        if not raw_kfs:
            raise ValueError('Go2 JSON: no keyframes found')

        keyframes = []
        for kf in raw_kfs:
            joints = kf.get('joints', NEUTRAL_POSE[:])
            if len(joints) < N_JOINTS:
                joints = joints + NEUTRAL_POSE[len(joints):]  # pad with neutral
            joints = [max(-3.0, min(3.0, float(j))) for j in joints[:N_JOINTS]]
            keyframes.append(Keyframe(
                time_ms=float(kf.get('time_ms', kf.get('t', 0))),
                joints=joints,
                metadata=kf.get('meta', {}),
            ))

        duration_ms = keyframes[-1].time_ms
        return AnimationClip(
            id=clip_id, name=data.get('name', name),
            source_format='json', duration_ms=duration_ms,
            keyframes=keyframes,
            loop=data.get('loop', False),
            blend_in_ms=float(data.get('blend_in_ms', 200)),
            blend_out_ms=float(data.get('blend_out_ms', 200)),
            tags=data.get('tags', []),
            metadata=data.get('metadata', {}),
        )

    @classmethod
    def _load_csv(cls, data: str, clip_id: str, name: str) -> AnimationClip:
        """
        Load CSV keyframe format.
        Header: time_ms, FR_0, FR_1, FR_2, FL_0, FL_1, FL_2, RR_0, RR_1, RR_2, RL_0, RL_1, RL_2
        Values: floats in radians (or degrees if header says _deg suffix)
        """
        reader = csv.DictReader(io.StringIO(data))
        keyframes = []
        use_degrees = any('_deg' in (h or '') for h in (reader.fieldnames or []))

        for row in reader:
            try:
                t_ms = float(row.get('time_ms', row.get('t', 0)))
                joints = []
                for jname in JOINT_NAMES:
                    val = float(row.get(jname, row.get(jname + '_deg', 0)))
                    if use_degrees:
                        val = math.radians(val)
                    joints.append(max(-3.0, min(3.0, val)))
                if len(joints) < N_JOINTS:
                    joints += NEUTRAL_POSE[len(joints):]
                keyframes.append(Keyframe(time_ms=t_ms, joints=joints[:N_JOINTS]))
            except (ValueError, KeyError):
                continue

        if not keyframes:
            raise ValueError('CSV: no valid rows parsed')

        return AnimationClip(
            id=clip_id, name=name, source_format='csv',
            duration_ms=keyframes[-1].time_ms, keyframes=keyframes,
        )

    @classmethod
    def to_funscript(cls, clip: AnimationClip) -> dict:
        """Export an AnimationClip back to FunScript format."""
        actions = []
        for kf in clip.keyframes:
            # Reverse-map from joint pose → FunScript pos [0-100]
            # Use RL_1 (rear left hip) as representative joint
            rl1_idx = JOINT_NAMES.index('RL_1')
            neutral_val = NEUTRAL_POSE[rl1_idx]
            engage_val = _ENGAGE_POSE[rl1_idx]
            actual = kf.joints[rl1_idx] if len(kf.joints) > rl1_idx else neutral_val
            if engage_val != neutral_val:
                pos = int(100 * (actual - neutral_val) / (engage_val - neutral_val))
            else:
                pos = 50
            pos = max(0, min(100, pos))
            actions.append({'at': int(kf.time_ms), 'pos': pos})
        return {
            'version': '1.0', 'range': 90, 'inverted': False,
            'metadata': {'title': clip.name, 'creator': 'Go2 Platform'},
            'actions': actions,
        }

    @classmethod
    def to_go2_json(cls, clip: AnimationClip) -> dict:
        """Export an AnimationClip to Go2 native JSON format."""
        return {
            'name': clip.name, 'loop': clip.loop,
            'blend_in_ms': clip.blend_in_ms, 'blend_out_ms': clip.blend_out_ms,
            'tags': clip.tags, 'source_format': clip.source_format,
            'keyframes': [
                {'time_ms': kf.time_ms, 'joints': [round(j, 4) for j in kf.joints]}
                for kf in clip.keyframes
            ],
        }


# ── Pose helpers ─────────────────────────────────────────────────────────

_ENGAGE_POSE = [
    0.0,  0.80, -1.5,   # FR: lower front
    0.0,  0.80, -1.5,   # FL
    0.0,  0.40, -0.9,   # RR: rear extended
    0.0,  0.40, -0.9,   # RL
]

_SIT_POSE = [
    0.0,  0.67, -1.3,   # FR: standing
    0.0,  0.67, -1.3,   # FL
    0.0,  1.60, -2.4,   # RR: folded
    0.0,  1.60, -2.4,   # RL
]

def _blend_poses(a: List[float], b: List[float], t: float) -> List[float]:
    """Linear blend between two poses."""
    t = max(0.0, min(1.0, t))
    return [ai + (bi - ai) * t for ai, bi in zip(a, b)]


# ════════════════════════════════════════════════════════════════════════════
# PROCEDURAL ANIMATIONS
# ════════════════════════════════════════════════════════════════════════════

class ProceduralAnimations:
    """
    Built-in procedural animations generated mathematically.
    No file loading needed — computed on-demand.
    """

    @staticmethod
    def breathing(duration_ms: float = 4000.0, amplitude: float = 0.02) -> AnimationClip:
        """Subtle body sway for idle 'breathing' animation."""
        steps = int(duration_ms / 50)   # 20fps
        kfs = []
        for i in range(steps + 1):
            t = i / steps
            phase = 2 * math.pi * t
            # Subtle hip and knee flex synchronized like breathing
            offset = amplitude * math.sin(phase)
            joints = [
                0.0, 0.67 + offset * 0.5, -1.3 - offset,   # FR
                0.0, 0.67 + offset * 0.5, -1.3 - offset,   # FL
                0.0, 0.67 + offset * 0.5, -1.3 - offset,   # RR
                0.0, 0.67 + offset * 0.5, -1.3 - offset,   # RL
            ]
            kfs.append(Keyframe(time_ms=i * 50, joints=joints))
        return AnimationClip(id='idle_breath', name='Breathing',
                             source_format='procedural', duration_ms=duration_ms,
                             keyframes=kfs, loop=True, priority=AnimPriority.IDLE,
                             blend_in_ms=500, blend_out_ms=500)

    @staticmethod
    def tail_wag(duration_ms: float = 2000.0) -> AnimationClip:
        """Happy tail-wag motion via rear hip oscillation."""
        steps = int(duration_ms / 33)   # 30fps
        kfs = []
        for i in range(steps + 1):
            t = i / steps
            phase = 4 * math.pi * t   # 2 full cycles
            rr_offset = 0.15 * math.sin(phase)
            rl_offset = 0.15 * math.sin(phase + math.pi)  # anti-phase
            joints = [
                0.0, 0.67, -1.3, 0.0, 0.67, -1.3,      # FR, FL unchanged
                rr_offset, 0.67, -1.3,                   # RR hip lateral
                rl_offset, 0.67, -1.3,                   # RL hip lateral
            ]
            kfs.append(Keyframe(time_ms=i * 33, joints=joints))
        return AnimationClip(id='tail_wag', name='Happy Wag',
                             source_format='procedural', duration_ms=duration_ms,
                             keyframes=kfs, loop=False, priority=AnimPriority.BEHAVIOR,
                             blend_in_ms=150, blend_out_ms=300)

    @staticmethod
    def head_tilt(duration_ms: float = 1500.0) -> AnimationClip:
        """Curious head-tilt via body roll oscillation."""
        steps = int(duration_ms / 33)
        kfs = []
        for i in range(steps + 1):
            t = i / steps
            # Smoothstep in, hold, smoothstep out
            if t < 0.2:
                tilt = t / 0.2
            elif t < 0.7:
                tilt = 1.0
            else:
                tilt = (1.0 - t) / 0.3

            tilt_smooth = tilt * tilt * (3 - 2 * tilt)
            offset = 0.12 * tilt_smooth
            # Left side lower slightly (asymmetric head tilt effect)
            joints = [
                0.0,          0.67, -1.3,     # FR normal
                -offset,      0.70, -1.3,     # FL slightly up (left tilt)
                0.0,          0.67, -1.3,     # RR normal
                -offset * 0.5, 0.67, -1.3,   # RL slight
            ]
            kfs.append(Keyframe(time_ms=i * 33, joints=joints))
        return AnimationClip(id='head_tilt', name='Head Tilt',
                             source_format='procedural', duration_ms=duration_ms,
                             keyframes=kfs, priority=AnimPriority.BEHAVIOR,
                             blend_in_ms=100, blend_out_ms=200)

    @staticmethod
    def sit_down(duration_ms: float = 1500.0) -> AnimationClip:
        """Smooth sit-down animation."""
        kfs = []
        n = 30
        for i in range(n + 1):
            t = i / n
            ts = t * t * (3 - 2 * t)   # smoothstep
            joints = _blend_poses(NEUTRAL_POSE, _SIT_POSE, ts)
            kfs.append(Keyframe(time_ms=(duration_ms / n) * i, joints=joints))
        return AnimationClip(id='sit_down', name='Sit Down',
                             source_format='procedural', duration_ms=duration_ms,
                             keyframes=kfs, priority=AnimPriority.BEHAVIOR,
                             blend_in_ms=100, blend_out_ms=50,
                             triggers={'on_complete': 'fsm:SIT'})

    @classmethod
    def all(cls) -> List[AnimationClip]:
        return [
            cls.breathing(), cls.tail_wag(), cls.head_tilt(), cls.sit_down(),
        ]


# ════════════════════════════════════════════════════════════════════════════
# ANIMATION BLENDER
# ════════════════════════════════════════════════════════════════════════════

class AnimationBlender:
    """
    Smooth transitions between animation clips.
    Supports: linear, cubic-smoothstep, and additive blending.
    """

    def __init__(self):
        self._from_clip:   Optional[AnimationClip] = None
        self._to_clip:     Optional[AnimationClip] = None
        self._blend_start: float = 0.0
        self._blend_dur:   float = 200.0  # ms
        self._mode:        BlendMode = BlendMode.CUBIC
        self._blending:    bool = False

    def start_blend(self, from_clip: Optional[AnimationClip],
                     to_clip: AnimationClip,
                     from_time_ms: float = 0.0):
        """Begin a blend transition."""
        self._from_clip   = from_clip
        self._to_clip     = to_clip
        self._blend_start = time.monotonic() * 1000.0
        self._blend_dur   = to_clip.blend_in_ms
        self._blending    = (from_clip is not None and self._blend_dur > 0)
        log.debug('Blend: %s → %s (%.0fms)',
                  from_clip.id if from_clip else 'none', to_clip.id, self._blend_dur)

    def sample(self, playback_ms: float) -> List[float]:
        """
        Sample blended joint positions.
        Returns list of N_JOINTS floats.
        """
        if not self._to_clip:
            return list(NEUTRAL_POSE)

        to_joints = self._to_clip.sample(playback_ms).joints

        if not self._blending or self._from_clip is None:
            return to_joints

        now_ms = time.monotonic() * 1000.0
        elapsed = now_ms - self._blend_start
        t = min(1.0, elapsed / max(self._blend_dur, 1.0))

        if t >= 1.0:
            self._blending = False
            return to_joints

        from_kf = self._from_clip.sample(playback_ms)
        from_joints = from_kf.joints

        if self._mode == BlendMode.CUBIC:
            ts = t * t * (3 - 2 * t)
        else:
            ts = t

        return [a + (b - a) * ts for a, b in zip(from_joints, to_joints)]

    @property
    def is_blending(self) -> bool:
        return self._blending


# ════════════════════════════════════════════════════════════════════════════
# ANIMATION PLAYER
# ════════════════════════════════════════════════════════════════════════════

class AnimationPlayer:
    """
    Real-time animation playback engine.
    Drives the blender at up to 200Hz, integrates with platform event bus.
    """

    def __init__(self, bus=None):
        self._bus       = bus
        self._clip:     Optional[AnimationClip] = None
        self._state:    AnimState = AnimState.IDLE
        self._start_t:  float = 0.0       # wall time when play started
        self._paused_at: float = 0.0      # elapsed time when paused
        self._speed:    float = 1.0
        self._blender  = AnimationBlender()
        self._current_joints: List[float] = list(NEUTRAL_POSE)
        self._callbacks: Dict[str, List[Callable]] = {'complete': [], 'loop': []}
        self._tick_task: Optional[asyncio.Task] = None
        self.frames_played: int = 0

    async def load(self, clip: AnimationClip):
        """Load a clip (with blend from current pose)."""
        prev_clip = self._clip
        self._clip = clip
        self._blender.start_blend(prev_clip, clip)
        self._state = AnimState.READY
        log.info('Animation loaded: %s (%.1fs)', clip.name, clip.duration_ms / 1000)
        if self._bus:
            await self._bus.emit('animation.loaded', clip.to_dict(), 'animation')

    async def play(self, speed: float = 1.0):
        """Start or resume playback."""
        if self._clip is None:
            log.warning('Animation: play() called with no clip loaded')
            return
        self._speed = max(0.1, min(10.0, speed))
        if self._state == AnimState.PAUSED:
            # Resume: adjust start time to account for pause
            pause_duration = time.monotonic() - self._paused_at
            self._start_t += pause_duration
        else:
            self._start_t = time.monotonic()

        self._state = AnimState.PLAYING
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())
        if self._bus:
            await self._bus.emit('animation.playing',
                                  {'clip': self._clip.id, 'speed': self._speed},
                                  'animation')

    async def pause(self):
        """Pause at current position."""
        if self._state == AnimState.PLAYING:
            self._paused_at = time.monotonic()
            self._state = AnimState.PAUSED
            if self._bus:
                await self._bus.emit('animation.paused',
                                      {'clip': self._clip.id if self._clip else None,
                                       'pos_ms': self._elapsed_ms()}, 'animation')

    async def stop(self):
        """Stop and reset to neutral pose with blend-out."""
        self._state = AnimState.IDLE
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None
        # Blend back to neutral
        neutral_clip = AnimationClip(
            id='neutral', name='Neutral', source_format='procedural',
            duration_ms=0, keyframes=[Keyframe(0, list(NEUTRAL_POSE))],
        )
        if self._clip:
            neutral_clip.blend_in_ms = self._clip.blend_out_ms
        self._blender.start_blend(self._clip, neutral_clip)
        self._clip = None
        if self._bus:
            await self._bus.emit('animation.stopped', {}, 'animation')

    def _elapsed_ms(self) -> float:
        if self._state == AnimState.PAUSED:
            return (self._paused_at - self._start_t) * 1000.0 * self._speed
        return (time.monotonic() - self._start_t) * 1000.0 * self._speed

    async def _tick_loop(self):
        """Inner tick — samples blender and fires joint updates at ~50Hz."""
        dt = 1.0 / 50.0  # 50Hz player loop (higher enough for smooth animation)
        while self._state in (AnimState.PLAYING, AnimState.BLENDING):
            try:
                elapsed_ms = self._elapsed_ms()
                self._current_joints = self._blender.sample(elapsed_ms)
                self.frames_played += 1

                # Check for clip completion
                if self._clip and elapsed_ms >= self._clip.duration_ms:
                    if self._clip.loop:
                        self._start_t = time.monotonic()
                        for cb in self._callbacks.get('loop', []):
                            cb(self._clip)
                        if self._bus:
                            await self._bus.emit('animation.loop',
                                                  {'clip': self._clip.id}, 'animation')
                    else:
                        self._state = AnimState.IDLE
                        for cb in self._callbacks.get('complete', []):
                            cb(self._clip)
                        # Fire FSM trigger if configured
                        trigger = self._clip.triggers.get('on_complete', '')
                        if trigger and self._bus:
                            await self._bus.emit('animation.trigger',
                                                  {'trigger': trigger,
                                                   'clip': self._clip.id}, 'animation')
                        if self._bus:
                            await self._bus.emit('animation.complete',
                                                  {'clip': self._clip.id}, 'animation')
                        self._clip = None
                        break

                await asyncio.sleep(dt)
            except asyncio.CancelledError:
                break

    def get_joints(self) -> List[float]:
        """Current joint positions — called by MotionController at 500Hz."""
        return list(self._current_joints)

    def on(self, event: str, callback: Callable):
        self._callbacks.setdefault(event, []).append(callback)

    def status(self) -> dict:
        elapsed = self._elapsed_ms() if self._state == AnimState.PLAYING else 0
        return {
            'state': self._state.name,
            'clip': self._clip.to_dict() if self._clip else None,
            'elapsed_ms': round(elapsed, 1),
            'speed': self._speed,
            'frames_played': self.frames_played,
            'is_blending': self._blender.is_blending,
        }


# ════════════════════════════════════════════════════════════════════════════
# ANIMATION REGISTRY
# ════════════════════════════════════════════════════════════════════════════

class AnimationRegistry:
    """
    Named animation library with lazy loading and in-memory caching.
    Pre-populated with procedural built-ins; plugins and users can register more.
    """

    def __init__(self):
        self._clips: Dict[str, AnimationClip] = {}
        self._load_builtins()

    def _load_builtins(self):
        for clip in ProceduralAnimations.all():
            self._clips[clip.id] = clip
        log.info('Animation registry: %d built-in clips loaded', len(self._clips))

    def register(self, clip: AnimationClip, overwrite: bool = False) -> bool:
        if clip.id in self._clips and not overwrite:
            log.warning('Animation: clip %r already registered', clip.id)
            return False
        self._clips[clip.id] = clip
        log.info('Animation registered: %s (%s)', clip.id, clip.source_format)
        return True

    def load_from_data(self, data: Any, clip_id: str, name: str = '',
                        fmt: Optional[str] = None) -> AnimationClip:
        """Load from raw data, register, and return clip."""
        clip = AnimationLoader.load(data, fmt=fmt, clip_id=clip_id, name=name or clip_id)
        self.register(clip, overwrite=True)
        return clip

    def get(self, clip_id: str) -> Optional[AnimationClip]:
        return self._clips.get(clip_id)

    def list(self) -> List[dict]:
        return [c.to_dict() for c in self._clips.values()]

    def remove(self, clip_id: str) -> bool:
        return bool(self._clips.pop(clip_id, None))


# ════════════════════════════════════════════════════════════════════════════
# ANIMATION STATE MACHINE
# ════════════════════════════════════════════════════════════════════════════

class AnimationStateMachine:
    """
    FSM-driven animation selection.
    Maps robot FSM states → animation clips with priority and override rules.
    Subscribes to EventBus for automatic transitions.
    """

    STATE_ANIMATIONS: Dict[str, str] = {
        'idle':        'idle_breath',
        'standing':    'idle_breath',
        'sitting':     'sit_down',
        'performing':  'idle_breath',
    }

    BEHAVIOR_ANIMATIONS: Dict[str, str] = {
        'tail_wag':    'tail_wag',
        'head_tilt':   'head_tilt',
        'sit':         'sit_down',
        'idle_breath': 'idle_breath',
    }

    def __init__(self, player: AnimationPlayer, registry: AnimationRegistry, bus=None):
        self._player   = player
        self._registry = registry
        self._bus      = bus
        self._active_state = 'offline'

        if bus:
            bus.subscribe('fsm.transition', self._on_fsm_transition)
            bus.subscribe('animation.trigger', self._on_animation_trigger)

    async def _on_fsm_transition(self, event: str, data: dict):
        """Automatically select animation when FSM state changes."""
        new_state = data.get('to', '')
        self._active_state = new_state
        clip_id = self.STATE_ANIMATIONS.get(new_state)
        if clip_id:
            clip = self._registry.get(clip_id)
            if clip:
                await self._player.load(clip)
                await self._player.play()

    async def _on_animation_trigger(self, event: str, data: dict):
        """Handle animation completion triggers (e.g., 'fsm:SIT')."""
        trigger = data.get('trigger', '')
        if trigger.startswith('fsm:') and self._bus:
            action = trigger[4:]
            await self._bus.emit('platform.command', {'action': action}, 'animation')

    async def play_behavior(self, behavior_id: str) -> bool:
        """Play the animation associated with a behavior."""
        clip_id = self.BEHAVIOR_ANIMATIONS.get(behavior_id, behavior_id)
        clip = self._registry.get(clip_id)
        if clip:
            await self._player.load(clip)
            await self._player.play()
            return True
        return False
