"""
cerberus/api/cerberus_routes.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS REST API Extension

Adds CERBERUS-specific endpoints to the Go2 Platform FastAPI server.
All cognitive, body, personality, and learning systems are accessible
through this module.

Mount on the main app:
    from cerberus.api.cerberus_routes import mount_cerberus_routes
    mount_cerberus_routes(app, cerberus_instance)

Endpoints:
  GET  /api/v1/cerberus/status         Full CERBERUS status
  GET  /api/v1/cerberus/mind           Cognitive state (memory, goals, attention)
  GET  /api/v1/cerberus/mind/memory    Working memory snapshot
  GET  /api/v1/cerberus/mind/goals     Goal stack status
  POST /api/v1/cerberus/mind/goals     Push a new goal
  DELETE /api/v1/cerberus/mind/goals/active  Complete/fail active goal
  GET  /api/v1/cerberus/mind/episodes  Episodic memory (recent events)
  GET  /api/v1/cerberus/body           Digital anatomy state
  GET  /api/v1/cerberus/body/joints    Per-joint state
  GET  /api/v1/cerberus/body/energy    Energy and fatigue state
  GET  /api/v1/cerberus/body/stability Stability assessment
  GET  /api/v1/cerberus/personality    Mood + traits + modulation
  POST /api/v1/cerberus/personality/event  Inject mood event
  PATCH /api/v1/cerberus/personality/traits  Adjust trait values
  GET  /api/v1/cerberus/learning       Learning system status
  POST /api/v1/cerberus/learning/prefer  Record behavior preference
  GET  /api/v1/cerberus/learning/suggest  Get behavior suggestion
  DELETE /api/v1/cerberus/learning/reset  Reset all learned preferences
  GET  /api/v1/cerberus/perception     Latest perception frame
  GET  /api/v1/cerberus/plugins        CERBERUS plugin status
  GET  /api/v1/cerberus/runtime        Runtime engine status
"""

import time
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger('cerberus.api')

try:
    from fastapi import APIRouter, HTTPException, Depends
    from pydantic import BaseModel, Field, field_validator
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    class BaseModel: pass


# ── Request models ────────────────────────────────────────────────────────

class GoalRequest(BaseModel):
    name:     str   = Field(..., min_length=1, max_length=128)
    type:     str   = Field(default='express')
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    params:   Dict[str, Any] = Field(default_factory=dict)
    deadline_s: Optional[float] = Field(default=None, ge=0, le=3600)

    @field_validator('type')
    @classmethod
    def valid_type(cls, v):
        allowed = {'explore','interact','patrol','express','rest','greet','custom'}
        if v not in allowed:
            raise ValueError(f'type must be one of {sorted(allowed)}')
        return v


class MoodEventRequest(BaseModel):
    event: str = Field(..., description='Event name from personality engine')
    magnitude: float = Field(default=1.0, ge=0.1, le=3.0)


class TraitAdjustRequest(BaseModel):
    openness:          Optional[float] = Field(None, ge=0.0, le=1.0)
    conscientiousness: Optional[float] = Field(None, ge=0.0, le=1.0)
    extraversion:      Optional[float] = Field(None, ge=0.0, le=1.0)
    agreeableness:     Optional[float] = Field(None, ge=0.0, le=1.0)
    neuroticism:       Optional[float] = Field(None, ge=0.0, le=1.0)


class PreferenceRequest(BaseModel):
    behavior_id: str   = Field(..., min_length=1, max_length=64)
    reward:      float = Field(default=1.0, ge=-2.0, le=2.0)
    source:      str   = Field(default='user')


# ════════════════════════════════════════════════════════════════════════════
# ROUTER FACTORY
# ════════════════════════════════════════════════════════════════════════════

def mount_cerberus_routes(app, cerberus, require_auth=None):
    """
    Attach all CERBERUS API routes to an existing FastAPI app.

    Args:
        app:          FastAPI application instance
        cerberus:     Cerberus instance (from cerberus/__init__.py)
        require_auth: Optional auth dependency (from main server)
    """
    if not HAS_FASTAPI:
        log.warning('FastAPI not available — CERBERUS routes not mounted')
        return

    router = APIRouter(prefix='/api/v1/cerberus', tags=['cerberus'])

    def get_cerberus():
        return cerberus

    auth_dep = [Depends(require_auth)] if require_auth else []

    # ── Status ────────────────────────────────────────────────────────────

    @router.get('/status')
    async def cerberus_status(c=Depends(get_cerberus)):
        """Full CERBERUS system status."""
        return c.status()

    @router.get('/runtime')
    async def runtime_status(c=Depends(get_cerberus)):
        """Runtime engine: tick stats, watchdogs, subsystems."""
        return c.runtime.status()

    # ── Cognitive Mind ────────────────────────────────────────────────────

    @router.get('/mind')
    async def mind_status(c=Depends(get_cerberus)):
        """Full cognitive state snapshot."""
        return c.mind.status()

    @router.get('/mind/memory')
    async def working_memory(c=Depends(get_cerberus)):
        """Working memory — current focus of attention."""
        return {
            'items':     c.mind.working_memory.snapshot(),
            'capacity':  c.mind.working_memory._capacity,
            'count':     len(c.mind.working_memory._items),
        }

    @router.get('/mind/episodes')
    async def episodic_memory(n: int = 20, event_type: Optional[str] = None,
                               c=Depends(get_cerberus)):
        """Episodic memory — recent experiences."""
        return {
            'recent':    c.mind.episodic_memory.recall_recent(min(n,100), event_type),
            'stats':     c.mind.episodic_memory.stats(),
        }

    @router.get('/mind/facts')
    async def semantic_memory(c=Depends(get_cerberus)):
        """Semantic memory — known world facts."""
        return {
            'facts': c.mind.semantic_memory.all_facts(),
            'count': len(c.mind.semantic_memory._facts),
        }

    @router.get('/mind/attention')
    async def attention_status(c=Depends(get_cerberus)):
        """Current attention targets and salience."""
        return c.mind.attention.status()

    @router.get('/mind/goals')
    async def goal_status(c=Depends(get_cerberus)):
        """Goal stack — active, pending, history."""
        return c.mind.goal_stack.status_dict()

    @router.post('/mind/goals', dependencies=auth_dep)
    async def push_goal(req: GoalRequest, c=Depends(get_cerberus)):
        """Push a goal onto the cognitive goal stack."""
        from cerberus.cognitive.mind import Goal
        deadline = time.time() + req.deadline_s if req.deadline_s else None
        goal = Goal(
            name=req.name, type=req.type,
            priority=req.priority, params=req.params,
            deadline=deadline,
        )
        goal_id = await c.mind.goal_stack.push(goal)
        return {'ok': True, 'goal_id': goal_id}

    @router.delete('/mind/goals/active', dependencies=auth_dep)
    async def complete_goal(success: bool = True, c=Depends(get_cerberus)):
        """Complete or fail the currently active goal."""
        await c.mind.goal_stack.complete_active(success=success)
        return {'ok': True, 'success': success}

    # ── Digital Anatomy ───────────────────────────────────────────────────

    @router.get('/body')
    async def body_state(c=Depends(get_cerberus)):
        """Full digital anatomy state."""
        return c.anatomy.body_state()

    @router.get('/body/joints')
    async def joint_states(c=Depends(get_cerberus)):
        """Per-joint kinematic and thermal state."""
        return {
            'joints':  c.anatomy.joints.all_states(),
            'summary': c.anatomy.joints.summary(),
        }

    @router.get('/body/energy')
    async def energy_state(c=Depends(get_cerberus)):
        """Battery level, power draw, fatigue state."""
        return {
            'energy':        c.anatomy.energy.state.to_dict(),
            'fatigue_label': c.anatomy.energy.fatigue_label,
            'velocity_cap':  round(c.anatomy.energy.velocity_cap_factor(), 3),
        }

    @router.get('/body/stability')
    async def stability_state(c=Depends(get_cerberus)):
        """ZMP stability assessment and tip-over risk."""
        return {
            'stability':    c.anatomy.stability.state.to_dict(),
            'is_safe':      c.anatomy.stability.is_safe(),
            'tip_over_risk': round(c.anatomy.stability.tip_over_risk(), 3),
        }

    # ── Personality Engine ────────────────────────────────────────────────

    @router.get('/personality')
    async def personality_state(c=Depends(get_cerberus)):
        """Current mood, personality traits, and behavior modulation."""
        return c.personality.status()

    @router.post('/personality/event', dependencies=auth_dep)
    async def inject_mood(req: MoodEventRequest, c=Depends(get_cerberus)):
        """Inject an emotional stimulus (e.g., 'successful_interaction')."""
        c.personality.inject_event(req.event, req.magnitude)
        return {
            'ok':   True,
            'mood': c.personality.mood.to_dict(),
        }

    @router.patch('/personality/traits', dependencies=auth_dep)
    async def adjust_traits(req: TraitAdjustRequest, c=Depends(get_cerberus)):
        """Directly set personality trait values."""
        t = c.personality.traits
        updates = {}
        for field_name, val in req.model_dump(exclude_none=True).items():
            setattr(t, field_name, val)
            updates[field_name] = val
        return {'ok': True, 'updated': updates, 'traits': t.to_dict()}

    @router.get('/personality/history')
    async def mood_history(n: int = 50, c=Depends(get_cerberus)):
        """Recent mood trajectory (arousal, valence over time)."""
        history = c.personality.mood.history[-n:]
        return {
            'history': [
                {'ts': ts, 'arousal': round(a, 3), 'valence': round(v, 3)}
                for ts, a, v in history
            ],
            'current': c.personality.mood.to_dict(),
        }

    # ── Learning System ───────────────────────────────────────────────────

    @router.get('/learning')
    async def learning_status(c=Depends(get_cerberus)):
        """Learning system status, Q-table stats, preferences."""
        return c.learning.status()

    @router.get('/learning/suggest')
    async def suggest_behavior(c=Depends(get_cerberus)):
        """Get a behavior suggestion from the combined RL + preference model."""
        suggestion = c.learning.suggest_behavior()
        top_prefs  = c.learning.preferences.preferred_behaviors(5)
        return {
            'suggestion':   suggestion,
            'top_prefs':    [{'behavior': b, 'weight': round(w, 4)} for b, w in top_prefs],
            'rl_q_states':  len(c.learning.rl._q),
        }

    @router.post('/learning/prefer', dependencies=auth_dep)
    async def record_preference(req: PreferenceRequest, c=Depends(get_cerberus)):
        """Record a behavior preference (user feedback to learning system)."""
        c.learning.register_user_behavior(req.behavior_id, req.reward)
        return {
            'ok':     True,
            'weight': round(c.learning.preferences.weight(req.behavior_id), 4),
        }

    @router.get('/learning/imitation')
    async def imitation_episodes(c=Depends(get_cerberus)):
        """List recorded imitation learning episodes."""
        return {
            'episodes': c.learning.imitation.list_episodes(),
            'count':    len(c.learning.imitation._episodes),
        }

    @router.post('/learning/imitation/record/start', dependencies=auth_dep)
    async def start_imitation_recording(name: str = 'new_sequence',
                                         c=Depends(get_cerberus)):
        """Start recording a user-demonstrated behavior sequence."""
        c.learning.imitation.start_recording(name=name)
        return {'ok': True, 'recording': True}

    @router.post('/learning/imitation/record/stop', dependencies=auth_dep)
    async def stop_imitation_recording(save: bool = True, c=Depends(get_cerberus)):
        """Stop recording and optionally save the episode."""
        ep_id = c.learning.imitation.stop_recording(save=save)
        return {'ok': True, 'episode_id': ep_id, 'saved': ep_id is not None}

    @router.delete('/learning/reset', dependencies=auth_dep)
    async def reset_learning(c=Depends(get_cerberus)):
        """Reset all learned preferences and Q-table (cannot be undone)."""
        c.learning.reset_all()
        return {'ok': True, 'message': 'All learning data cleared'}

    # ── Perception ────────────────────────────────────────────────────────

    @router.get('/perception')
    async def perception_frame(c=Depends(get_cerberus)):
        """Latest perception frame (objects, humans, scene, occupancy)."""
        if hasattr(c, 'perception') and c.perception:
            return c.perception.current_frame.to_dict()
        return {'error': 'Perception pipeline not active'}

    @router.get('/perception/map')
    async def occupancy_map(c=Depends(get_cerberus)):
        """Downsampled 2D occupancy grid (20×20 cells)."""
        if hasattr(c, 'perception') and c.perception:
            grid = c.perception._mapper.grid_snapshot(downsample=5)
            return {
                'grid':        grid,
                'cell_size_m': 0.5,
                'robot_pose':  list(c.perception.current_frame.robot_pose),
            }
        return {'error': 'Perception pipeline not active'}

    # ── CERBERUS Plugins ──────────────────────────────────────────────────

    @router.get('/plugins')
    async def cerberus_plugins(c=Depends(get_cerberus)):
        """CERBERUS plugin registry status."""
        if hasattr(c, 'plugin_registry') and c.plugin_registry:
            return c.plugin_registry.status()
        return {'plugins': [], 'total': 0}

    # Mount router onto app
    app.include_router(router)
    log.info('CERBERUS API routes mounted: %d endpoints',
             len([r for r in router.routes]))
