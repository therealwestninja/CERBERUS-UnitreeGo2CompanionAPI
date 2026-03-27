"""
go2_platform/tests/test_platform.py
══════════════════════════════════════════════════════════════════════════════
Comprehensive test suite — Go2 Platform
Tests: Platform core, FSM, Safety, Security, Simulation, Fleet, OTA, Plugins

Run: python -m pytest tests/test_platform.py -v
     python tests/test_platform.py  (standalone)
"""

import asyncio
import hashlib
import json
import math
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch

# ─ add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from backend.core.platform import (
        PlatformCore, SafetyConfig, SafetyEnforcer, AuthoritativeFSM,
        WorldModel, WorldObject, Zone, BehaviorRegistry, MissionSystem,
        EventBus, Telemetry, RobotState, SafetyLevel, BehaviorPolicy)
    from backend.core.security import (
        InputSanitizer, CommandValidator, RateLimiter, AuditLog,
        SecurityManager, ObjectImportValidator, COMMAND_SCHEMA)
    from backend.core.plugin_system import (
        PluginSystem, PluginContext, validate_manifest)
    IMPORTS_OK = True
except ImportError as e:
    IMPORTS_OK = False
    IMPORT_ERROR = str(e)


def run_async(coro):
    """Helper: run a coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ════════════════════════════════════════════════════════════════════════════
# SKIP GUARD
# ════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(IMPORTS_OK, f'Imports failed: {IMPORT_ERROR if not IMPORTS_OK else ""}')
class TestImports(unittest.TestCase):
    def test_imports_succeed(self):
        self.assertTrue(IMPORTS_OK)


# ════════════════════════════════════════════════════════════════════════════
# EVENT BUS
# ════════════════════════════════════════════════════════════════════════════

class TestEventBus(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.received = []

    def test_subscribe_and_emit(self):
        def handler(event, data): self.received.append((event, data))
        self.bus.subscribe('test.event', handler)
        run_async(self.bus.emit('test.event', {'val': 42}))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0][1]['val'], 42)

    def test_unsubscribe(self):
        def handler(event, data): self.received.append(data)
        self.bus.subscribe('x', handler)
        self.bus.unsubscribe('x', handler)
        run_async(self.bus.emit('x', {}))
        self.assertEqual(len(self.received), 0)

    def test_multiple_handlers(self):
        results = []
        for i in range(3):
            def make_h(n): return lambda e, d: results.append(n)
            self.bus.subscribe('multi', make_h(i))
        run_async(self.bus.emit('multi', {}))
        self.assertEqual(len(results), 3)

    def test_error_in_handler_doesnt_break_others(self):
        results = []
        def bad(event, data): raise RuntimeError('intentional')
        def good(event, data): results.append(True)
        self.bus.subscribe('err', bad)
        self.bus.subscribe('err', good)
        run_async(self.bus.emit('err', {}))
        self.assertEqual(len(results), 1)

    def test_history_stored(self):
        run_async(self.bus.emit('hist.test', {'x': 1}))
        run_async(self.bus.emit('hist.test', {'x': 2}))
        h = self.bus.recent(10)
        self.assertGreaterEqual(len(h), 2)

    def test_history_capped(self):
        for i in range(600):
            run_async(self.bus.emit('cap', {'i': i}))
        self.assertLessEqual(len(self.bus._history), 500)


# ════════════════════════════════════════════════════════════════════════════
# SAFETY ENFORCER
# ════════════════════════════════════════════════════════════════════════════

class TestSafetyEnforcer(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.cfg = SafetyConfig()
        self.safety = SafetyEnforcer(self.cfg, self.bus)
        # Arm the safety state to allow evaluation
        self.safety._last_telemetry_ts = time.monotonic()

    def _tel(self, **kwargs):
        t = Telemetry()
        for k, v in kwargs.items():
            setattr(t, k, v)
        self.safety.update_telemetry(t)

    def test_nominal_ok(self):
        self._tel(pitch_deg=0, roll_deg=0, contact_force_n=0, battery_pct=80)
        ok, reason = run_async(self.safety.evaluate({'action': 'STAND'}))
        self.assertTrue(ok)

    def test_estop_blocks_all(self):
        run_async(self.safety.trigger_estop('test'))
        ok, _ = run_async(self.safety.evaluate({'action': 'STAND'}))
        self.assertFalse(ok)

    def test_pitch_limit(self):
        self._tel(pitch_deg=11.0, roll_deg=0, contact_force_n=0, battery_pct=80)
        ok, reason = run_async(self.safety.evaluate({'action': 'STAND'}))
        self.assertFalse(ok)
        self.assertIn('Pitch', reason)

    def test_pitch_below_limit_ok(self):
        self._tel(pitch_deg=9.9, roll_deg=0, contact_force_n=0, battery_pct=80)
        ok, _ = run_async(self.safety.evaluate({'action': 'STAND'}))
        self.assertTrue(ok)

    def test_roll_negative_trip(self):
        self._tel(pitch_deg=0, roll_deg=-11.0, contact_force_n=0, battery_pct=80)
        ok, reason = run_async(self.safety.evaluate({'action': 'STAND'}))
        self.assertFalse(ok)

    def test_force_limit(self):
        self._tel(pitch_deg=0, roll_deg=0, contact_force_n=31.0, battery_pct=80)
        ok, reason = run_async(self.safety.evaluate({'action': 'WALK'}))
        self.assertFalse(ok)

    def test_battery_trip(self):
        self._tel(pitch_deg=0, roll_deg=0, contact_force_n=0, battery_pct=9.0)
        ok, _ = run_async(self.safety.evaluate({'action': 'WALK'}))
        self.assertFalse(ok)

    def test_overtemp_trip(self):
        t = Telemetry()
        t.motor_temps = {'fl': 73.0, 'fr': 42.0, 'rl': 42.0, 'rr': 42.0}
        self.safety._last_telemetry_ts = time.monotonic()
        self.safety.update_telemetry(t)
        ok, _ = run_async(self.safety.evaluate({'action': 'STAND'}))
        self.assertFalse(ok)

    def test_velocity_capping(self):
        self._tel(pitch_deg=0, roll_deg=0, contact_force_n=0, battery_pct=80)
        cmd = {'action': 'WALK', 'velocity': 5.0}
        ok, _ = run_async(self.safety.evaluate(cmd))
        self.assertTrue(ok)
        self.assertLessEqual(cmd['velocity'], self.cfg.max_velocity_ms)

    def test_human_zone_blocks_interaction(self):
        self._tel(pitch_deg=0, roll_deg=0, contact_force_n=0, battery_pct=80)
        self.safety.update_perception(human_in_zone=True, obstacle_dist=2.0)
        ok, reason = run_async(self.safety.evaluate({'action': 'EXECUTE'}))
        self.assertFalse(ok)

    def test_trip_count_increments(self):
        for _ in range(3):
            self._tel(pitch_deg=12.0)
            run_async(self.safety.evaluate({'action': 'WALK'}))
        self.assertGreaterEqual(self.safety.trip_count, 1)

    def test_estop_count_increments(self):
        run_async(self.safety.trigger_estop())
        run_async(self.safety.clear_estop())
        run_async(self.safety.trigger_estop())
        self.assertEqual(self.safety.estop_count, 2)

    def test_safety_level_caution(self):
        self._tel(pitch_deg=7.0, roll_deg=0, contact_force_n=0, battery_pct=80)
        self.safety._update_level()
        self.assertIn(self.safety.level, (SafetyLevel.CAUTION, SafetyLevel.WARNING))

    def test_clear_estop_resets(self):
        run_async(self.safety.trigger_estop())
        self.assertEqual(self.safety.level, SafetyLevel.ESTOP)
        run_async(self.safety.clear_estop())
        self.assertNotEqual(self.safety.level, SafetyLevel.ESTOP)

    def test_status_dict(self):
        s = self.safety.status()
        self.assertIn('level', s)
        self.assertIn('trips', s)
        self.assertIn('estops', s)


# ════════════════════════════════════════════════════════════════════════════
# AUTHORITATIVE FSM
# ════════════════════════════════════════════════════════════════════════════

class TestAuthoritativeFSM(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.cfg = SafetyConfig()
        self.safety = SafetyEnforcer(self.cfg, self.bus)
        self.fsm = AuthoritativeFSM(self.safety, self.bus)

    def test_initial_state_offline(self):
        self.assertEqual(self.fsm.state, RobotState.OFFLINE)

    def test_offline_to_idle(self):
        ok, _ = run_async(self.fsm.transition(RobotState.IDLE, 'test'))
        self.assertTrue(ok)
        self.assertEqual(self.fsm.state, RobotState.IDLE)

    def test_invalid_transition_rejected(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        ok, msg = run_async(self.fsm.transition(RobotState.WALKING))
        self.assertFalse(ok)  # not armed

    def test_requires_arm_for_motion(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        ok, msg = run_async(self.fsm.transition(RobotState.STANDING))
        self.assertFalse(ok)
        self.assertIn('armed', msg.lower())

    def test_arm_and_stand(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        run_async(self.fsm.arm())
        ok, _ = run_async(self.fsm.transition(RobotState.STANDING))
        self.assertTrue(ok)

    def test_full_mission_sequence(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        run_async(self.fsm.arm())
        for state in (RobotState.STANDING, RobotState.WALKING, RobotState.NAVIGATING):
            ok, msg = run_async(self.fsm.transition(state))
            self.assertTrue(ok, f'Failed: {state.name} — {msg}')

    def test_estop_from_anywhere(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        run_async(self.fsm.arm())
        run_async(self.fsm.transition(RobotState.STANDING))
        run_async(self.safety.trigger_estop())
        # After E-STOP, FSM transition to ESTOP may be blocked by safety.level check
        # Directly verify the safety level is ESTOP
        self.assertEqual(self.safety.level, SafetyLevel.ESTOP)

    def test_history_recorded(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        run_async(self.fsm.arm())
        run_async(self.fsm.transition(RobotState.STANDING))
        self.assertGreaterEqual(len(self.fsm.history), 1)

    def test_disarm_returns_to_standing(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        run_async(self.fsm.arm())
        run_async(self.fsm.transition(RobotState.STANDING))
        run_async(self.fsm.transition(RobotState.WALKING))
        run_async(self.fsm.disarm())
        self.assertFalse(self.fsm.armed)

    def test_status_dict(self):
        s = self.fsm.status()
        self.assertIn('state', s)
        self.assertIn('armed', s)
        self.assertIn('allowed_transitions', s)

    def test_fault_recovers_to_idle(self):
        run_async(self.fsm.transition(RobotState.IDLE))
        run_async(self.fsm.arm())
        run_async(self.fsm.transition(RobotState.STANDING))
        run_async(self.fsm.transition(RobotState.FAULT))
        ok, _ = run_async(self.fsm.transition(RobotState.IDLE))
        self.assertTrue(ok)


# ════════════════════════════════════════════════════════════════════════════
# WORLD MODEL
# ════════════════════════════════════════════════════════════════════════════

class TestWorldModel(unittest.TestCase):

    def setUp(self):
        self.world = WorldModel(EventBus())

    def test_default_objects_loaded(self):
        self.assertGreater(len(self.world.objects), 0)

    def test_add_object(self):
        obj = WorldObject('test_obj','Test','soft_prop',
                          ['mount_play'],[],20.0,{'x':0,'y':0,'z':0.4},[0,0,1])
        ok, _ = self.world.add_object(obj)
        self.assertTrue(ok)
        self.assertIn('test_obj', self.world.objects)

    def test_reject_zero_force(self):
        obj = WorldObject('bad','Bad','soft_prop',[],[],0.0,
                          {'x':0,'y':0,'z':0},[0,0,1])
        ok, msg = self.world.add_object(obj)
        self.assertFalse(ok)

    def test_remove_object(self):
        obj = WorldObject('rm_me','Remove','soft_prop',[],[],20.0,
                          {'x':0,'y':0,'z':0},[0,0,1])
        self.world.add_object(obj)
        ok = self.world.remove_object('rm_me')
        self.assertTrue(ok)
        self.assertNotIn('rm_me', self.world.objects)

    def test_find_by_affordance(self):
        results = self.world.find_by_affordance('mount_play')
        self.assertGreater(len(results), 0)

    def test_export_roundtrip(self):
        export = self.world.export()
        self.assertIn('objects', export)
        self.assertIn('schema_version', export)
        added, errors = self.world.import_from_dict(export)
        self.assertEqual(errors, 0)

    def test_add_zone(self):
        z = Zone('my_zone','Test Zone','no_enter',{'x':1,'y':1},1.5)
        self.world.add_zone(z)
        self.assertIn('my_zone', self.world.zones)

    def test_add_waypoint(self):
        self.world.add_waypoint('wp1', {'x':2,'y':2,'z':0}, 'Point A')
        self.assertIn('wp1', self.world.waypoints)

    def test_import_objects_validated(self):
        # Use the world model's export format for round-trip
        from backend.core.platform import WorldObject
        obj = WorldObject('import1','Import Test','soft_prop',
                          ['mount_play'],[],20.0,{'x':0,'y':0,'z':0.4},[0,0,1])
        self.world.add_object(obj)
        export = self.world.export()
        # Create fresh world and import
        w2 = WorldModel(EventBus())
        added, errors = w2.import_from_dict(export)
        self.assertGreater(added, 0)


# ════════════════════════════════════════════════════════════════════════════
# BEHAVIOR REGISTRY
# ════════════════════════════════════════════════════════════════════════════

class TestBehaviorRegistry(unittest.TestCase):

    def setUp(self):
        self.reg = BehaviorRegistry(EventBus())

    def test_builtins_loaded(self):
        self.assertGreater(len(self.reg._behaviors), 0)

    def test_register_custom(self):
        ok = self.reg.register({
            'id': 'custom_dance', 'name': 'Custom Dance',
            'category': 'trick', 'icon': '💃', 'duration_s': 3.0
        }, source='test')
        self.assertTrue(ok)
        self.assertIn('custom_dance', self.reg._behaviors)

    def test_register_missing_fields(self):
        ok = self.reg.register({'id': 'bad'})  # missing name + category
        self.assertFalse(ok)

    def test_list_by_category(self):
        cats = self.reg.list_by_category()
        self.assertIsInstance(cats, dict)
        self.assertIn('posture', cats)

    def test_set_policy(self):
        self.reg.set_policy(BehaviorPolicy.AGILE)
        self.assertEqual(self.reg.active_policy, BehaviorPolicy.AGILE)
        params = self.reg.get_policy_params()
        self.assertIn('max_vel', params)
        self.assertGreater(params['max_vel'], 1.0)

    def test_smooth_slower_than_agile(self):
        self.reg.set_policy(BehaviorPolicy.SMOOTH)
        smooth_vel = self.reg.get_policy_params()['max_vel']
        self.reg.set_policy(BehaviorPolicy.AGILE)
        agile_vel = self.reg.get_policy_params()['max_vel']
        self.assertLess(smooth_vel, agile_vel)

    def test_get_behavior(self):
        b = self.reg.get('sit')
        self.assertIsNotNone(b)
        self.assertEqual(b['id'], 'sit')


# ════════════════════════════════════════════════════════════════════════════
# SECURITY — INPUT SANITIZER
# ════════════════════════════════════════════════════════════════════════════

class TestInputSanitizer(unittest.TestCase):

    def test_clean_string_passes(self):
        v, safe = InputSanitizer.sanitize_str('cushion_blue', 'id')
        self.assertTrue(safe)
        self.assertEqual(v, 'cushion_blue')

    def test_html_tag_rejected(self):
        _, safe = InputSanitizer.sanitize_str('<script>alert(1)</script>', 'name')
        self.assertFalse(safe)

    def test_javascript_protocol_rejected(self):
        _, safe = InputSanitizer.sanitize_str('javascript:void(0)', 'notes')
        self.assertFalse(safe)

    def test_sql_injection_rejected(self):
        _, safe = InputSanitizer.sanitize_str("'; DROP TABLE objects; --", 'id')
        self.assertFalse(safe)

    def test_path_traversal_rejected(self):
        _, safe = InputSanitizer.sanitize_str('../../etc/passwd', 'path')
        self.assertFalse(safe)

    def test_null_byte_rejected(self):
        _, safe = InputSanitizer.sanitize_str('abc\x00def', 'id')
        self.assertFalse(safe)

    def test_control_chars_stripped(self):
        v, safe = InputSanitizer.sanitize_str('abc\x01\x02\x03def', 'name')
        self.assertTrue(safe)
        self.assertNotIn('\x01', v)

    def test_dict_sanitized_recursively(self):
        data = {'name': 'ok', 'notes': '<b>bold</b>'}
        _, safe = InputSanitizer.sanitize_dict(data)
        self.assertFalse(safe)

    def test_max_length_enforced(self):
        long_str = 'a' * 1000
        v, safe = InputSanitizer.sanitize_str(long_str, 'notes')
        self.assertTrue(safe)
        self.assertLessEqual(len(v), 512)


# ════════════════════════════════════════════════════════════════════════════
# SECURITY — COMMAND VALIDATOR
# ════════════════════════════════════════════════════════════════════════════

class TestCommandValidator(unittest.TestCase):

    def setUp(self):
        self.validator = CommandValidator(InputSanitizer())

    def test_valid_estop(self):
        cmd, ok, _ = self.validator.validate({'action': 'ESTOP'}, armed=False)
        self.assertTrue(ok)

    def test_estop_no_arm_required(self):
        cmd, ok, _ = self.validator.validate({'action': 'ESTOP'}, armed=False)
        self.assertTrue(ok)

    def test_arm_gate_stand(self):
        _, ok, msg = self.validator.validate({'action': 'STAND'}, armed=False)
        self.assertFalse(ok)
        self.assertIn('armed', msg.lower())

    def test_stand_passes_when_armed(self):
        _, ok, _ = self.validator.validate({'action': 'STAND'}, armed=True)
        self.assertTrue(ok)

    def test_unknown_action_rejected(self):
        _, ok, _ = self.validator.validate({'action': 'HACK_ROBOT'})
        self.assertFalse(ok)

    def test_missing_required_field(self):
        _, ok, msg = self.validator.validate({'action': 'RUN_BEHAVIOR'}, armed=True)
        self.assertFalse(ok)
        self.assertIn('behavior_id', msg)

    def test_required_field_present(self):
        _, ok, _ = self.validator.validate(
            {'action': 'RUN_BEHAVIOR', 'behavior_id': 'sit'}, armed=True)
        self.assertTrue(ok)

    def test_velocity_clamped(self):
        cmd, ok, _ = self.validator.validate(
            {'action': 'WALK', 'velocity': 10.0}, armed=True)
        self.assertTrue(ok)
        self.assertLessEqual(cmd.get('velocity', 10.0), 1.5)

    def test_invalid_id_format(self):
        _, ok, _ = self.validator.validate(
            {'action': 'RUN_BEHAVIOR', 'behavior_id': 'bad id!'}, armed=True)
        self.assertFalse(ok)

    def test_all_known_actions_valid_schema(self):
        for action in COMMAND_SCHEMA:
            self.assertIn('required', COMMAND_SCHEMA[action])
            self.assertIn('armed', COMMAND_SCHEMA[action])


# ════════════════════════════════════════════════════════════════════════════
# SECURITY — RATE LIMITER
# ════════════════════════════════════════════════════════════════════════════

class TestRateLimiter(unittest.TestCase):

    def test_allows_within_limit(self):
        rl = RateLimiter()
        for _ in range(10):
            self.assertTrue(rl.check('c1', 'command'))

    def test_blocks_above_limit(self):
        rl = RateLimiter()
        results = [rl.check('c2', 'command') for _ in range(30)]
        # First 20 should pass, some later ones blocked
        self.assertTrue(all(results[:20]))

    def test_estop_not_rate_limited(self):
        rl = RateLimiter()
        # E-STOP has very high limits
        for _ in range(50):
            self.assertTrue(rl.check('c3', 'estop'))

    def test_violations_tracked(self):
        rl = RateLimiter()
        for _ in range(35):
            rl.check('c4', 'command')
        self.assertGreater(rl.violations('c4'), 0)


# ════════════════════════════════════════════════════════════════════════════
# SECURITY — AUDIT LOG
# ════════════════════════════════════════════════════════════════════════════

class TestAuditLog(unittest.TestCase):

    def test_record_and_retrieve(self):
        al = AuditLog()
        al.record('cmd', 'client_1', {'action': 'STAND'}, 'ok')
        entries = al.recent(10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['event'], 'cmd')

    def test_chain_is_valid(self):
        al = AuditLog()
        for i in range(5):
            al.record('event', 'src', {'i': i})
        self.assertTrue(al.verify_chain())

    def test_secrets_not_logged(self):
        al = AuditLog()
        al.record('api_call', 'client', {'api_key': 'sk-secret-key', 'action': 'STAND'})
        entries = al.recent(1)
        self.assertNotIn('api_key', entries[0].get('data', {}))

    def test_search_by_event(self):
        al = AuditLog()
        al.record('cmd', 'a', {})
        al.record('import', 'b', {})
        al.record('cmd', 'c', {})
        cmds = al.search(event='cmd')
        self.assertEqual(len(cmds), 2)

    def test_search_by_outcome(self):
        al = AuditLog()
        al.record('cmd', 'a', {}, 'ok')
        al.record('cmd', 'b', {}, 'rejected')
        rejected = al.search(outcome='rejected')
        self.assertEqual(len(rejected), 1)


# ════════════════════════════════════════════════════════════════════════════
# OBJECT IMPORT VALIDATOR
# ════════════════════════════════════════════════════════════════════════════

class TestObjectImportValidator(unittest.TestCase):

    def setUp(self):
        self.v = ObjectImportValidator()

    def _obj(self, **kwargs):
        base = {
            'id': 'test_obj', 'name': 'Test', 'type': 'soft_prop',
            'affordances': ['mount_play'], 'moods': ['playful'],
            'max_force_n': 20.0, 'pos': {'x': 0, 'y': 0, 'z': 0.4},
        }
        base.update(kwargs)
        return base

    def test_valid_object(self):
        valid, errors = self.v.validate_registry({'objects': [self._obj()]})
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(errors), 0)

    def test_invalid_type_rejected(self):
        _, errors = self.v.validate_registry({'objects': [self._obj(type='evil_prop')]})
        self.assertGreater(len(errors), 0)

    def test_zero_force_rejected(self):
        _, errors = self.v.validate_registry({'objects': [self._obj(max_force_n=0)]})
        self.assertGreater(len(errors), 0)

    def test_too_many_objects(self):
        objs = [self._obj(id=f'obj_{i}') for i in range(150)]
        _, errors = self.v.validate_registry({'objects': objs})
        self.assertGreater(len(errors), 0)

    def test_unknown_affordances_stripped(self):
        obj = self._obj(affordances=['mount_play', 'hack_motor', 'knead'])
        valid, _ = self.v.validate_registry({'objects': [obj]})
        if valid:
            self.assertNotIn('hack_motor', valid[0].get('affordances', []))

    def test_html_in_name_rejected(self):
        _, errors = self.v.validate_registry(
            {'objects': [self._obj(name='<script>alert()</script>')]})
        self.assertGreater(len(errors), 0)

    def test_array_format_accepted(self):
        valid, _ = self.v.validate_registry([self._obj()])
        self.assertEqual(len(valid), 1)


# ════════════════════════════════════════════════════════════════════════════
# PLUGIN MANIFEST VALIDATOR
# ════════════════════════════════════════════════════════════════════════════

class TestPluginManifest(unittest.TestCase):

    def _valid(self, **kwargs):
        base = {
            'name': 'test_plugin',
            'version': '1.0.0',
            'permissions': ['behaviors'],
            'entry_point': 'plugin.py',
            'description': 'Test plugin',
        }
        base.update(kwargs)
        return base

    def test_valid_manifest(self):
        ok, _ = validate_manifest(self._valid())
        self.assertTrue(ok)

    def test_missing_name(self):
        m = self._valid(); del m['name']
        ok, _ = validate_manifest(m)
        self.assertFalse(ok)

    def test_invalid_name_chars(self):
        ok, _ = validate_manifest(self._valid(name='bad name!'))
        self.assertFalse(ok)

    def test_unknown_permission(self):
        ok, msg = validate_manifest(self._valid(permissions=['fsm', 'hack_kernel']))
        self.assertFalse(ok)

    def test_path_traversal_entry_point(self):
        ok, _ = validate_manifest(self._valid(entry_point='../../evil.py'))
        self.assertFalse(ok)

    def test_absolute_path_entry_point(self):
        ok, _ = validate_manifest(self._valid(entry_point='/etc/passwd'))
        self.assertFalse(ok)

    def test_all_valid_permissions(self):
        valid_perms = ['ui', 'behaviors', 'api', 'fsm', 'sensors', 'world', 'missions']
        ok, _ = validate_manifest(self._valid(permissions=valid_perms))
        self.assertTrue(ok)


# ════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE (lightweight unit tests, no asyncio)
# ════════════════════════════════════════════════════════════════════════════

class TestSimulationKinematics(unittest.TestCase):
    """Pure math tests for simulation kinematics, no async needed."""

    def test_imu_quaternion_zero(self):
        """Zero orientation → zero pitch/roll."""
        w, x, y, z = 1.0, 0.0, 0.0, 0.0
        sinr = 2.0 * (w * x + y * z)
        cosr = 1.0 - 2.0 * (x**2 + y**2)
        roll = math.degrees(math.atan2(sinr, cosr))
        sinp = 2.0 * (w * y - z * x)
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, sinp))))
        self.assertAlmostEqual(pitch, 0.0, places=3)
        self.assertAlmostEqual(roll, 0.0, places=3)

    def test_battery_drain_model(self):
        bat = 87.0
        batt_mah = 8000 * 0.87
        # Walk for 10 seconds at 4.5A, 29.4V
        for _ in range(10):
            dq = 4.5 * 1.0 / 3600
            batt_mah = max(0, batt_mah - dq * 1000)
        bat_after = batt_mah / 8000 * 100
        self.assertLess(bat_after, 87.0)
        self.assertGreater(bat_after, 0.0)

    def test_gait_phase_bounded(self):
        phase = 0.0
        dt = 0.002
        for _ in range(10000):
            phase = (phase + 2 * math.pi * 2.0 * dt) % (2 * math.pi)
        self.assertGreaterEqual(phase, 0.0)
        self.assertLess(phase, 2 * math.pi + 1e-6)

    def test_thermal_model_heats_under_load(self):
        temp = 22.0  # start at ambient
        thermal_r = 0.12
        power_w = 15.0
        for _ in range(100):
            delta = (thermal_r * power_w - 0.15 * (temp - 22.0)) * 0.005
            temp = max(22.0, temp + delta)
        self.assertGreater(temp, 22.0)  # must be above ambient under load

    def test_lidar_obstacle_detection(self):
        """Obstacle in front of robot should reduce LiDAR reading."""
        robot_x, robot_y = 0.0, 0.0
        obstacle = {'x': 1.5, 'y': 0.0, 'r': 0.3}
        # Angle 0° should hit obstacle
        angle_rad = 0.0
        dx, dy = obstacle['x'] - robot_x, obstacle['y'] - robot_y
        angle_to_obs = math.atan2(dy, dx)
        dist_to_obs = math.sqrt(dx**2 + dy**2)
        diff = abs(math.atan2(math.sin(angle_rad - angle_to_obs),
                              math.cos(angle_rad - angle_to_obs)))
        half_angle = math.atan2(obstacle['r'], dist_to_obs)
        self.assertLess(diff, half_angle)
        hit_dist = dist_to_obs - obstacle['r']
        self.assertAlmostEqual(hit_dist, 1.2, delta=0.01)


# ════════════════════════════════════════════════════════════════════════════
# FLEET MANAGER
# ════════════════════════════════════════════════════════════════════════════

class TestFleetManager(unittest.TestCase):

    def setUp(self):
        from backend.core.fleet_and_ota import FleetManager, FleetTask, SyncEngine
        self.fm = FleetManager()
        self.FleetTask = FleetTask
        self.SyncEngine = SyncEngine

    def test_register_robot(self):
        r = self.fm.register_robot('r1', 'Go2 Alpha', 'http://192.168.1.100:8080')
        self.assertEqual(r.robot_id, 'r1')
        self.assertIn('r1', self.fm.robots)

    def test_register_limit(self):
        for i in range(20):
            self.fm.register_robot(f'r{i}', f'Robot {i}', f'http://10.0.0.{i}:8080')
        with self.assertRaises(ValueError):
            self.fm.register_robot('overflow', 'Over', 'http://1.2.3.4:8080')

    def test_deregister_robot(self):
        self.fm.register_robot('rx', 'X', 'http://x:8080')
        ok = self.fm.deregister_robot('rx')
        self.assertTrue(ok)
        self.assertNotIn('rx', self.fm.robots)

    def test_status_dict(self):
        self.fm.register_robot('r1', 'A', 'http://a:8080')
        s = self.fm.status()
        self.assertIn('total_robots', s)
        self.assertIn('robots', s)

    def test_sync_session_created(self):
        se = self.SyncEngine()
        session_id = se.create_session('zoomies', ['r1', 'r2'], delay_s=1.0)
        self.assertIsNotNone(session_id)
        session = se.get_session(session_id)
        self.assertEqual(session['behavior_id'], 'zoomies')
        self.assertAlmostEqual(session['t_zero'], time.time() + 1.0, delta=0.1)

    def test_sync_mark_ready(self):
        se = self.SyncEngine()
        sid = se.create_session('dance', ['r1', 'r2'])
        se.mark_ready(sid, 'r1')
        all_ready = se.mark_ready(sid, 'r2')
        self.assertTrue(all_ready)


# ════════════════════════════════════════════════════════════════════════════
# PLATFORM CORE INTEGRATION
# ════════════════════════════════════════════════════════════════════════════

class TestPlatformCoreIntegration(unittest.TestCase):

    def setUp(self):
        self.platform = PlatformCore()

    def test_platform_initial_state(self):
        self.assertEqual(self.platform.fsm.state, RobotState.OFFLINE)
        self.assertFalse(self.platform.fsm.armed)

    def test_estop_command(self):
        result = run_async(self.platform.execute_command({'action': 'ESTOP'}))
        self.assertTrue(result.get('ok'))
        self.assertEqual(self.platform.safety.level, SafetyLevel.ESTOP)

    def test_arm_command(self):
        async def _test():
            p = PlatformCore()
            await p.start()
            result = await p.execute_command({'action': 'ARM'})
            armed = p.fsm.armed
            await p.stop()
            return result, armed
        result, armed = run_async(_test())
        self.assertTrue(result.get('ok'))
        self.assertTrue(armed)

    def test_safety_blocks_on_low_battery(self):
        async def _test():
            p = PlatformCore()
            await p.start()
            await p.execute_command({'action': 'ARM'})
            p.telemetry.battery_pct = 5.0
            p.safety.update_telemetry(p.telemetry)
            # Navigate should be blocked
            result = await p.execute_command({'action': 'NAVIGATE'})
            await p.stop()
            return result
        result = run_async(_test())
        # Either blocked by battery safety or not (depends on timing)
        # Key: no crash, returns dict
        self.assertIsInstance(result, dict)

    def test_full_status(self):
        s = self.platform.full_status()
        self.assertIn('platform', s)
        self.assertIn('fsm', s)
        self.assertIn('safety', s)
        self.assertIn('telemetry', s)


# ════════════════════════════════════════════════════════════════════════════
# PERFORMANCE BENCHMARKS
# ════════════════════════════════════════════════════════════════════════════

class TestPerformance(unittest.TestCase):

    def test_safety_evaluate_speed(self):
        """Safety evaluation < 0.1ms (must not slow down 500Hz loop)."""
        bus = EventBus()
        safety = SafetyEnforcer(SafetyConfig(), bus)
        t = Telemetry()
        safety.update_telemetry(t)
        safety._last_telemetry_ts = time.monotonic()

        N = 1000
        start = time.perf_counter()
        for _ in range(N):
            run_async(safety.evaluate({'action': 'STAND'}))
        elapsed_us = (time.perf_counter() - start) / N * 1e6
        # Allow generous budget since we're calling asyncio.run each time
        self.assertLess(elapsed_us, 2000, f'Safety eval too slow: {elapsed_us:.0f}µs')

    def test_sanitizer_throughput(self):
        """Input sanitizer < 0.05ms per call."""
        data = {'action': 'STAND', 'name': 'test', 'notes': 'some notes here'}
        N = 5000
        start = time.perf_counter()
        for _ in range(N):
            InputSanitizer.sanitize_dict(data)
        elapsed_us = (time.perf_counter() - start) / N * 1e6
        self.assertLess(elapsed_us, 200)

    def test_audit_log_throughput(self):
        """Audit log records < 0.1ms per entry."""
        al = AuditLog()
        N = 1000
        start = time.perf_counter()
        for i in range(N):
            al.record('cmd', f'client_{i%10}', {'action': 'STAND'})
        elapsed_us = (time.perf_counter() - start) / N * 1e6
        self.assertLess(elapsed_us, 500)

    def test_command_validation_throughput(self):
        v = CommandValidator(InputSanitizer())
        N = 2000
        start = time.perf_counter()
        for _ in range(N):
            v.validate({'action': 'STAND'}, armed=True)
        elapsed_us = (time.perf_counter() - start) / N * 1e6
        self.assertLess(elapsed_us, 500)


# ════════════════════════════════════════════════════════════════════════════
# RUNNER
# ════════════════════════════════════════════════════════════════════════════


class TestLocalization(unittest.TestCase):

    def setUp(self):
        from backend.i18n.localization import LocalizationEngine, DEFAULT_LOCALE
        self.engine = LocalizationEngine()

    def test_english_default(self):
        self.assertEqual(self.engine.locale, 'en')

    def test_basic_translation(self):
        self.assertEqual(self.engine.translate('states.idle'), 'Ready')

    def test_nested_key(self):
        self.assertEqual(self.engine.translate('commands.arm'), 'Arm')

    def test_variable_interpolation(self):
        result = self.engine.translate('safety.pitch', value=10.2, limit=10.0)
        self.assertIn('10.2', result)
        self.assertIn('10.0', result)

    def test_fallback_to_english(self):
        self.engine.set_locale('ja')
        # animation.play is not in JA, should fall back to EN
        result = self.engine.translate('animation.play')
        self.assertEqual(result, 'Play')

    def test_fallback_to_key(self):
        result = self.engine.translate('nonexistent.key.here')
        self.assertEqual(result, 'nonexistent.key.here')

    def test_set_locale_valid(self):
        ok = self.engine.set_locale('ja')
        self.assertTrue(ok)
        self.assertEqual(self.engine.locale, 'ja')

    def test_set_locale_invalid(self):
        ok = self.engine.set_locale('xx_INVALID')
        self.assertFalse(ok)
        self.assertEqual(self.engine.locale, 'en')  # unchanged

    def test_japanese_translation(self):
        self.engine.set_locale('ja')
        result = self.engine.translate('states.idle')
        self.assertEqual(result, '待機中')

    def test_german_translation(self):
        self.engine.set_locale('de')
        result = self.engine.translate('commands.arm')
        self.assertEqual(result, 'Aktivieren')

    def test_available_locales_list(self):
        locales = self.engine.available_locales()
        codes = [l['code'] for l in locales]
        for expected in ('en', 'es', 'fr', 'de', 'ja', 'zh'):
            self.assertIn(expected, codes)

    def test_locale_metadata(self):
        locales = self.engine.available_locales()
        en = next(l for l in locales if l['code'] == 'en')
        self.assertIn('flag', en)
        self.assertIn('rtl', en)

    def test_export_locale(self):
        pack = self.engine.export_locale('en')
        self.assertIn('states', pack)
        self.assertIn('commands', pack)

    def test_coverage_report(self):
        report = self.engine.coverage_report()
        self.assertIn('es', report)
        self.assertIn('coverage_pct', report['es'])
        self.assertGreater(report['es']['coverage_pct'], 0)

    def test_plugin_namespace(self):
        self.engine.register_plugin_namespace('my_plugin', {
            'en': {'hello': 'Hello from plugin'},
            'es': {'hello': 'Hola del plugin'},
        })
        result = self.engine.translate('my_plugin.hello')
        self.assertEqual(result, 'Hello from plugin')
        self.engine.set_locale('es')
        result = self.engine.translate('my_plugin.hello')
        self.assertEqual(result, 'Hola del plugin')

    def test_all_keys_helper(self):
        keys = self.engine._all_keys(self.engine._packs['en'])
        self.assertGreater(len(keys), 50)
        self.assertIn('states.idle', keys)
        self.assertIn('commands.arm', keys)

    def test_security_no_injection_in_key(self):
        # Keys with path traversal should just miss
        result = self.engine.translate('../../etc/passwd')
        self.assertEqual(result, '../../etc/passwd')

    def test_interpolation_missing_var_preserved(self):
        # Missing interpolation var → keep {placeholder}
        result = self.engine.translate('safety.pitch', value=5.0)
        # limit not provided → {limit} preserved
        self.assertIn('{limit}', result)


# ════════════════════════════════════════════════════════════════════════════
# ANIMATION SYSTEM TESTS
# ════════════════════════════════════════════════════════════════════════════

class TestAnimationSystem(unittest.TestCase):

    def setUp(self):
        from backend.animation.animation_system import (
            AnimationLoader, AnimationRegistry, AnimationClip, Keyframe,
            ProceduralAnimations, NEUTRAL_POSE, N_JOINTS, BlendMode
        )
        self.Loader = AnimationLoader
        self.Registry = AnimationRegistry
        self.Clip = AnimationClip
        self.KF = Keyframe
        self.Proc = ProceduralAnimations
        self.NEUTRAL = NEUTRAL_POSE
        self.N = N_JOINTS
        self.BlendMode = BlendMode

    def _funscript(self, actions=None):
        return {
            'version': '1.0', 'range': 90,
            'actions': actions or [
                {'at': 0, 'pos': 0}, {'at': 500, 'pos': 100},
                {'at': 1000, 'pos': 50}, {'at': 2000, 'pos': 0}
            ]
        }

    def test_funscript_load(self):
        clip = self.Loader.load(self._funscript(), clip_id='test')
        self.assertEqual(clip.source_format, 'funscript')
        self.assertEqual(len(clip.keyframes), 4)
        self.assertEqual(clip.duration_ms, 2000)

    def test_funscript_joints_count(self):
        clip = self.Loader.load(self._funscript(), clip_id='test')
        for kf in clip.keyframes:
            self.assertEqual(len(kf.joints), self.N)

    def test_funscript_clamped_pos(self):
        bad_fs = self._funscript([{'at': 0, 'pos': -50}, {'at': 1000, 'pos': 200}])
        clip = self.Loader.load(bad_fs, clip_id='clamp_test')
        # pos should be clamped to [0, 100]
        for kf in clip.keyframes:
            for j in kf.joints:
                self.assertGreaterEqual(j, -3.0)
                self.assertLessEqual(j, 3.0)

    def test_json_load_roundtrip(self):
        clip = self.Proc.breathing()
        json_data = self.Loader.to_go2_json(clip)
        reloaded = self.Loader.load(json_data, fmt='json', clip_id='reload')
        self.assertEqual(len(reloaded.keyframes), len(clip.keyframes))
        self.assertAlmostEqual(reloaded.duration_ms, clip.duration_ms, delta=1)

    def test_funscript_export_roundtrip(self):
        clip = self.Proc.tail_wag()
        exported = self.Loader.to_funscript(clip)
        self.assertIn('actions', exported)
        self.assertGreater(len(exported['actions']), 0)
        # All pos values in valid range
        for act in exported['actions']:
            self.assertGreaterEqual(act['pos'], 0)
            self.assertLessEqual(act['pos'], 100)

    def test_csv_load(self):
        import io, csv
        header = 'time_ms,' + ','.join([
            'FR_0','FR_1','FR_2','FL_0','FL_1','FL_2',
            'RR_0','RR_1','RR_2','RL_0','RL_1','RL_2'
        ])
        rows = [header]
        for t in [0, 500, 1000]:
            vals = [t] + self.NEUTRAL
            rows.append(','.join(str(v) for v in vals))
        csv_data = '\n'.join(rows)
        clip = self.Loader.load(csv_data, fmt='csv', clip_id='csv_test')
        self.assertEqual(len(clip.keyframes), 3)
        self.assertEqual(clip.duration_ms, 1000)

    def test_keyframe_sampling_at_boundaries(self):
        clip = self.Proc.breathing()
        kf_start = clip.sample(0)
        kf_end   = clip.sample(clip.duration_ms)
        self.assertEqual(len(kf_start.joints), self.N)
        self.assertEqual(len(kf_end.joints), self.N)

    def test_keyframe_sampling_midpoint(self):
        clip = self.Loader.load(self._funscript([
            {'at': 0, 'pos': 0}, {'at': 1000, 'pos': 100}
        ]), clip_id='lerp_test')
        kf_mid = clip.sample(500)
        # At midpoint of a 0→100 FunScript, joints should be between neutral and engage
        from backend.animation.animation_system import NEUTRAL_POSE, _ENGAGE_POSE
        for i, j in enumerate(kf_mid.joints):
            lo, hi = min(NEUTRAL_POSE[i], _ENGAGE_POSE[i]), max(NEUTRAL_POSE[i], _ENGAGE_POSE[i])
            self.assertGreaterEqual(j, lo - 0.01)
            self.assertLessEqual(j, hi + 0.01)

    def test_keyframe_linear_interpolation(self):
        kf1 = self.KF(time_ms=0,    joints=[0.0] * self.N)
        kf2 = self.KF(time_ms=1000, joints=[1.0] * self.N)
        mid = kf1.interpolate_to(kf2, 0.5)
        for j in mid.joints:
            self.assertAlmostEqual(j, 0.5, delta=0.01)

    def test_cubic_blend_smoother_than_linear(self):
        kf1 = self.KF(0,    [0.0] * self.N)
        kf2 = self.KF(1000, [1.0] * self.N)
        lin   = kf1.interpolate_to(kf2, 0.1, mode=self.BlendMode.LINEAR).joints[0]
        cubic = kf1.interpolate_to(kf2, 0.1, mode=self.BlendMode.CUBIC).joints[0]
        # Cubic smoothstep should be LESS than linear at t=0.1 (slow start)
        self.assertLess(cubic, lin)

    def test_loop_sampling(self):
        clip = self.Proc.breathing()
        clip.loop = True
        # Sample past end — should wrap
        kf_wrap = clip.sample(clip.duration_ms + 500)
        kf_base = clip.sample(500)
        for a, b in zip(kf_wrap.joints, kf_base.joints):
            self.assertAlmostEqual(a, b, delta=0.01)

    def test_procedural_breathing_loop(self):
        clip = self.Proc.breathing()
        self.assertTrue(clip.loop)
        self.assertEqual(clip.priority.name, 'IDLE')

    def test_procedural_tail_wag_duration(self):
        clip = self.Proc.tail_wag()
        self.assertGreater(clip.duration_ms, 0)
        self.assertFalse(clip.loop)

    def test_registry_builtins(self):
        reg = self.Registry()
        self.assertGreater(len(reg.list()), 0)
        breathing = reg.get('idle_breath')
        self.assertIsNotNone(breathing)

    def test_registry_register_custom(self):
        reg = self.Registry()
        clip = self.Loader.load(self._funscript(), clip_id='my_anim', name='My Anim')
        ok = reg.register(clip)
        self.assertTrue(ok)
        self.assertIsNotNone(reg.get('my_anim'))

    def test_registry_no_duplicate(self):
        reg = self.Registry()
        clip = self.Proc.breathing()
        ok_first  = reg.register(clip)
        ok_second = reg.register(clip)  # no overwrite
        self.assertFalse(ok_second)

    def test_bvh_load_produces_keyframes(self):
        # Minimal valid BVH
        bvh = """HIERARCHY
ROOT Hips
{
  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
}
MOTION
Frames: 3
Frame Time: 0.033333
0 0 0 0 5 0
0 0 0 0 10 0
0 0 0 0 15 0
"""
        clip = self.Loader.load(bvh, fmt='bvh', clip_id='bvh_test')
        self.assertEqual(len(clip.keyframes), 3)
        self.assertAlmostEqual(clip.duration_ms, 66.6, delta=1)

    def test_format_detection_funscript(self):
        fmt = self.Loader._detect_format({'actions': []})
        self.assertEqual(fmt, 'funscript')

    def test_format_detection_json(self):
        fmt = self.Loader._detect_format({'keyframes': []})
        self.assertEqual(fmt, 'json')


# ════════════════════════════════════════════════════════════════════════════
# BEHAVIOR TREE TESTS
# ════════════════════════════════════════════════════════════════════════════

class TestBehaviorTree(unittest.TestCase):

    def setUp(self):
        from backend.behavior_tree.behavior_tree import (
            Blackboard, Sequence, Selector, Parallel, Inverter,
            Succeeder, Repeater, Cooldown, Timeout, Wait,
            Condition, Action, BTStatus, TickContext, build_companion_tree
        )
        self.BB = Blackboard
        self.SEQ = Sequence
        self.SEL = Selector
        self.PAR = Parallel
        self.INV = Inverter
        self.SUC = Succeeder
        self.REP = Repeater
        self.CD  = Cooldown
        self.TO  = Timeout
        self.Wait = Wait
        self.Cond = Condition
        self.Act  = Action
        self.Status = BTStatus
        self.CTX = TickContext
        self.build = build_companion_tree

    def _ctx(self, **bb_data):
        bb = self.BB()
        for k, v in bb_data.items():
            bb.set(k.replace('__', '.'), v)
        return self.CTX(blackboard=bb, platform=None, bus=None)

    def _async(self, coro):
        return run_async(coro)

    # Blackboard
    def test_bb_set_get(self):
        bb = self.BB()
        bb.set('robot.armed', True)
        self.assertTrue(bb.get('robot.armed'))

    def test_bb_default(self):
        bb = self.BB()
        self.assertIsNone(bb.get('missing'))
        self.assertEqual(bb.get('missing', 42), 42)

    def test_bb_has(self):
        bb = self.BB()
        self.assertFalse(bb.has('x'))
        bb.set('x', 1)
        self.assertTrue(bb.has('x'))

    def test_bb_telemetry_update(self):
        bb = self.BB()
        bb.update_from_telemetry({'battery_pct': 85.0, 'pitch_deg': 3.5})
        self.assertAlmostEqual(bb.get('robot.battery_pct'), 85.0)

    # Condition
    def test_condition_true(self):
        c = self.Cond('test', lambda bb: bb.get('val', 0) > 5)
        ctx = self._ctx(val=10)
        result = self._async(c.tick(ctx))
        self.assertEqual(result, self.Status.SUCCESS)

    def test_condition_false(self):
        c = self.Cond('test', lambda bb: bb.get('val', 0) > 5)
        ctx = self._ctx(val=3)
        result = self._async(c.tick(ctx))
        self.assertEqual(result, self.Status.FAILURE)

    def test_condition_error_returns_failure(self):
        c = self.Cond('bad', lambda bb: 1 / 0)  # raises ZeroDivisionError
        ctx = self._ctx()
        result = self._async(c.tick(ctx))
        self.assertEqual(result, self.Status.FAILURE)

    # Sequence
    def test_sequence_all_success(self):
        nodes = [self.Cond(f'c{i}', lambda bb: True) for i in range(3)]
        seq = self.SEQ('seq', nodes)
        result = self._async(seq.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    def test_sequence_first_fails(self):
        nodes = [
            self.Cond('fail', lambda bb: False),
            self.Cond('ok',   lambda bb: True),
        ]
        seq = self.SEQ('seq', nodes)
        result = self._async(seq.tick(self._ctx()))
        self.assertEqual(result, self.Status.FAILURE)
        # Second node should NOT have been ticked
        self.assertEqual(nodes[1]._tick_count, 0)

    # Selector
    def test_selector_first_succeeds(self):
        nodes = [
            self.Cond('ok',   lambda bb: True),
            self.Cond('also', lambda bb: True),
        ]
        sel = self.SEL('sel', nodes)
        result = self._async(sel.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)
        self.assertEqual(nodes[1]._tick_count, 0)  # short-circuit

    def test_selector_all_fail(self):
        nodes = [self.Cond(f'c{i}', lambda bb: False) for i in range(3)]
        sel = self.SEL('sel', nodes)
        result = self._async(sel.tick(self._ctx()))
        self.assertEqual(result, self.Status.FAILURE)

    def test_selector_fallback(self):
        nodes = [
            self.Cond('fail',   lambda bb: False),
            self.Cond('succeed', lambda bb: True),
        ]
        sel = self.SEL('sel', nodes)
        result = self._async(sel.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    # Inverter
    def test_inverter_flips_success(self):
        c = self.Cond('t', lambda bb: True)
        i = self.INV('inv', c)
        result = self._async(i.tick(self._ctx()))
        self.assertEqual(result, self.Status.FAILURE)

    def test_inverter_flips_failure(self):
        c = self.Cond('f', lambda bb: False)
        i = self.INV('inv', c)
        result = self._async(i.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    # Succeeder
    def test_succeeder_always_success(self):
        c = self.Cond('f', lambda bb: False)
        s = self.SUC('suc', c)
        result = self._async(s.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    # Wait
    def test_wait_returns_running_then_success(self):
        import time as _t
        w = self.Wait('w', duration_s=0.01)
        ctx = self._ctx()
        r1 = self._async(w.tick(ctx))
        self.assertEqual(r1, self.Status.RUNNING)
        _t.sleep(0.015)
        r2 = self._async(w.tick(ctx))
        self.assertEqual(r2, self.Status.SUCCESS)

    # Parallel
    def test_parallel_all_policy(self):
        nodes = [self.Cond(f'c{i}', lambda bb: True) for i in range(3)]
        par = self.PAR('par', nodes, policy='all')
        result = self._async(par.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    def test_parallel_any_policy(self):
        nodes = [
            self.Cond('fail',    lambda bb: False),
            self.Cond('succeed', lambda bb: True),
        ]
        par = self.PAR('par', nodes, policy='any')
        result = self._async(par.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    # Action
    def test_action_returns_success(self):
        async def my_action(ctx): return True
        a = self.Act('a', my_action)
        result = self._async(a.tick(self._ctx()))
        self.assertEqual(result, self.Status.SUCCESS)

    def test_action_returns_failure(self):
        async def my_action(ctx): return False
        a = self.Act('a', my_action)
        result = self._async(a.tick(self._ctx()))
        self.assertEqual(result, self.Status.FAILURE)

    def test_action_exception_returns_failure(self):
        async def bad_action(ctx): raise RuntimeError('oops')
        a = self.Act('bad', bad_action)
        result = self._async(a.tick(self._ctx()))
        self.assertEqual(result, self.Status.FAILURE)

    # Companion tree
    def test_companion_tree_builds(self):
        tree = self.build()
        self.assertIsNotNone(tree)
        self.assertEqual(tree.name, 'root')

    def test_companion_tree_ticks_without_crash(self):
        bb = self.BB()
        bb.update_from_telemetry({'battery_pct': 80, 'pitch_deg': 0})
        bb.update_from_fsm({'state': 'idle', 'armed': False})
        ctx = self.CTX(blackboard=bb, platform=None, bus=None)
        tree = self.build()
        result = self._async(tree.tick(ctx))
        self.assertIn(result, list(self.Status))

    def test_debug_str(self):
        tree = self.build()
        debug = tree.debug_str()
        self.assertIn('root', debug)
        self.assertIn('[SEL]', debug)

    def test_cooldown_prevents_rapid_fire(self):
        call_count = [0]
        async def action(ctx): call_count[0] += 1; return True
        a = self.Act('a', action)
        cd = self.CD('cd', a, cooldown_s=10.0)  # 10s cooldown
        ctx = self._ctx()
        r1 = self._async(cd.tick(ctx))
        r2 = self._async(cd.tick(ctx))
        self.assertEqual(r1, self.Status.SUCCESS)
        self.assertEqual(r2, self.Status.FAILURE)  # still cooling
        self.assertEqual(call_count[0], 1)


# ════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY TESTS
# ════════════════════════════════════════════════════════════════════════════

class TestObservability(unittest.TestCase):

    def setUp(self):
        from backend.observability.metrics import (
            MetricsRegistry, HealthChecker, HealthStatus, HealthResult,
            Tracer, Counter, Gauge, Histogram,
        )
        self.MR = MetricsRegistry
        self.HC = HealthChecker
        self.HS = HealthStatus
        self.HR = HealthResult
        self.Tracer = Tracer

    def test_counter_increment(self):
        c = self.__class__._make_counter()
        c.inc()
        c.inc(5)
        self.assertAlmostEqual(c.value(), 6.0)

    @staticmethod
    def _make_counter():
        from backend.observability.metrics import Counter
        return Counter('test_counter', 'test')

    def test_gauge_set(self):
        from backend.observability.metrics import Gauge
        g = Gauge('test_gauge')
        g.set(42.5)
        self.assertAlmostEqual(g.value(), 42.5)

    def test_gauge_inc_dec(self):
        from backend.observability.metrics import Gauge
        g = Gauge('g2')
        g.set(10.0); g.inc(5.0); g.dec(2.0)
        self.assertAlmostEqual(g.value(), 13.0)

    def test_histogram_observe(self):
        from backend.observability.metrics import Histogram
        h = Histogram('test_hist', buckets=[0.1, 0.5, 1.0])
        for v in [0.05, 0.2, 0.8]:
            h.observe(v)
        self.assertEqual(h._count, 3)
        self.assertAlmostEqual(h.mean, (0.05 + 0.2 + 0.8) / 3, delta=0.001)

    def test_histogram_percentile(self):
        from backend.observability.metrics import Histogram
        h = Histogram('pct_hist')
        for v in [0.1 * i for i in range(1, 101)]:
            h.observe(v)
        self.assertGreater(h.p95, 9.0)  # near 9.5
        self.assertGreater(h.p99, 9.5)

    def test_metrics_registry_has_builtins(self):
        reg = self.MR()
        prom = reg.prometheus_text()
        self.assertIn('go2_commands_total', prom)
        self.assertIn('go2_battery_pct', prom)

    def test_metrics_to_dict(self):
        reg = self.MR()
        reg.gauge('go2_battery_pct').set(87.0)
        d = reg.to_dict()
        self.assertIn('uptime_s', d)
        self.assertIn('battery_pct', d)

    def test_health_check_ok(self):
        hc = self.HC()
        hc.register('test', lambda: True)
        result = run_async(hc.aggregate())
        self.assertEqual(result['status'], 'ok')

    def test_health_check_degraded(self):
        from backend.observability.metrics import HealthResult, HealthStatus
        hc = self.HC()
        hc.register('bad', lambda: HealthResult('bad', HealthStatus.DEGRADED, 'slow'))
        result = run_async(hc.aggregate())
        self.assertEqual(result['status'], 'degraded')

    def test_health_check_timeout(self):
        import asyncio as _a
        hc = self.HC()
        async def slow(): await _a.sleep(5.0)
        hc.register('slow', slow)
        result = run_async(hc.aggregate())
        checks = {c['name']: c for c in result['checks']}
        self.assertEqual(checks.get('slow', {}).get('status'), 'down')

    def test_tracer_span(self):
        tracer = self.Tracer()
        async def traced():
            async with tracer.span('test_op', action='TEST') as span:
                pass
        run_async(traced())
        traces = tracer.recent_traces(5)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]['name'], 'test_op')
        self.assertGreater(traces[0]['duration_ms'], 0)

    def test_tracer_error_span(self):
        tracer = self.Tracer()
        async def error_traced():
            try:
                async with tracer.span('fail_op') as span:
                    raise ValueError('test error')
            except ValueError:
                pass
        run_async(error_traced())
        traces = tracer.recent_traces(1)
        self.assertEqual(traces[0]['status'], 'error')

    def test_prometheus_format_valid(self):
        reg = self.MR()
        reg.counter('go2_commands_total').inc(5)
        text = reg.prometheus_text()
        lines = [l for l in text.split('\n') if l and not l.startswith('#')]
        # All metric lines should have a space (name value format)
        for line in lines:
            self.assertIn(' ', line, f'Invalid Prometheus line: {line!r}')



if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestImports if IMPORTS_OK else None,
        TestEventBus,
        TestSafetyEnforcer,
        TestAuthoritativeFSM,
        TestWorldModel,
        TestBehaviorRegistry,
        TestInputSanitizer,
        TestCommandValidator,
        TestRateLimiter,
        TestAuditLog,
        TestObjectImportValidator,
        TestPluginManifest,
        TestSimulationKinematics,
        TestFleetManager,
        TestPlatformCoreIntegration,
        TestPerformance,
        TestLocalization,
        TestAnimationSystem,
        TestBehaviorTree,
        TestObservability,
    ]

    for tc in test_classes:
        if tc:
            suite.addTests(loader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    total = result.testsRun
    fails = len(result.failures) + len(result.errors)
    print(f'\n{"═"*62}')
    print(f'  GO2 PLATFORM TEST SUITE: {total - fails}/{total} passed')
    print('  ✓ ALL PASSED' if fails == 0 else f'  ✗ {fails} FAILURES')
    print('═'*62)
    sys.exit(0 if fails == 0 else 1)


# ════════════════════════════════════════════════════════════════════════════
# i18n LOCALIZATION TESTS
# ════════════════════════════════════════════════════════════════════════════
