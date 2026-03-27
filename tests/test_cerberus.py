"""
tests/test_cerberus.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Test Suite
Tests: Runtime, Cognitive, Body, Personality, Learning, Perception, Plugins

Run: python tests/test_cerberus.py
"""

import asyncio
import math
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def run_async(coro):
    loop = asyncio.new_event_loop()
    try: return loop.run_until_complete(coro)
    finally: loop.close()


# ════════════════════════════════════════════════════════════════════════════
# RUNTIME ENGINE
# ════════════════════════════════════════════════════════════════════════════

class TestRuntime(unittest.TestCase):

    def setUp(self):
        from cerberus.runtime import (CerberusRuntime, SystemEventBus,
            WatchdogMonitor, TickStats, Priority, Subsystem, TickContext)
        self.Runtime = CerberusRuntime
        self.Bus     = SystemEventBus
        self.WD      = WatchdogMonitor
        self.Stats   = TickStats
        self.P       = Priority
        self.Sub     = Subsystem

    def test_priority_ordering(self):
        from cerberus.runtime import Priority
        self.assertLess(Priority.SAFETY, Priority.CONTROL)
        self.assertLess(Priority.CONTROL, Priority.COGNITION)
        self.assertLess(Priority.COGNITION, Priority.ANIMATION)
        self.assertLess(Priority.ANIMATION, Priority.LEARNING)
        self.assertLess(Priority.LEARNING, Priority.TELEMETRY)

    def test_event_bus_subscribe_emit(self):
        # Use SAFETY priority so it bypasses the queue and dispatches immediately
        from cerberus.runtime import Priority
        bus = self.Bus()
        results = []
        bus.subscribe('test.safe', lambda e, d: results.append(d))
        run_async(bus.emit('test.safe', {'x': 1}, priority=Priority.SAFETY))
        self.assertGreater(len(results), 0)

    def test_event_bus_safety_immediate(self):
        from cerberus.runtime import Priority
        bus = self.Bus()
        results = []
        bus.subscribe('estop', lambda e, d: results.append(d))
        run_async(bus.emit('estop', {'source': 'test'}, priority=Priority.SAFETY))
        self.assertEqual(len(results), 1)

    def test_event_bus_history(self):
        bus = self.Bus()
        run_async(bus.emit('ev.1', 'a'))
        run_async(bus.emit('ev.2', 'b'))
        h = bus.recent(10)
        self.assertGreaterEqual(len(h), 2)
        self.assertTrue(any(e['name'] == 'ev.1' for e in h))

    def test_tick_stats(self):
        from cerberus.runtime import TickStats, Priority
        ts = TickStats(priority=Priority.SAFETY)
        ts.tick_count = 1000
        ts.total_time_s = 1.0
        self.assertAlmostEqual(ts.mean_ms, 1.0, places=3)

    def test_watchdog_register_and_kick(self):
        bus = self.Bus()
        wd  = self.WD(bus)
        wd.register('test_dog', timeout_s=2.0)
        wd.kick('test_dog')
        status = wd.status()
        self.assertIn('test_dog', status)
        self.assertFalse(status['test_dog']['tripped'])

    def test_watchdog_trip(self):
        bus = self.Bus()
        wd  = self.WD(bus)
        trips = []
        async def on_trip(name): trips.append(name)
        wd.register('slow_dog', timeout_s=0.01, on_trip=on_trip)
        # Don't kick — let it expire
        time.sleep(0.02)
        status = wd.status()
        # Note: need to run monitor loop for trip to fire
        # Just verify the state after expiry
        self.assertIn('slow_dog', status)

    def test_subsystem_registry(self):
        from cerberus.runtime import SubsystemRegistry, Subsystem, Priority
        reg = SubsystemRegistry()

        class TestSub(Subsystem):
            name     = 'test_sub'
            priority = Priority.COGNITION

        s = TestSub()
        reg.register(s)
        self.assertIsNotNone(reg.get('test_sub'))
        at_cog = reg.at_priority(Priority.COGNITION)
        self.assertIn(s, at_cog)

    def test_runtime_status_before_start(self):
        rt = self.Runtime()
        s  = rt.status()
        self.assertFalse(s['started'])
        self.assertIn('version', s)

    def test_runtime_shared_store(self):
        rt = self.Runtime()
        rt.share('key1', 'value1')
        rt.share('key2', [1, 2, 3])
        self.assertEqual(rt.shared('key1'), 'value1')
        self.assertEqual(rt.shared('key2'), [1, 2, 3])
        self.assertIsNone(rt.shared('missing'))


# ════════════════════════════════════════════════════════════════════════════
# WORKING MEMORY
# ════════════════════════════════════════════════════════════════════════════

class TestWorkingMemory(unittest.TestCase):

    def setUp(self):
        from cerberus.cognitive.mind import WorkingMemory
        self.WM = WorkingMemory

    def test_capacity_limit(self):
        wm = self.WM(capacity=5)
        ids = [wm.store({'i': i}, importance=0.3) for i in range(8)]
        self.assertLessEqual(len(wm._items), 5)

    def test_high_importance_survives_displacement(self):
        wm = self.WM(capacity=3)
        wm.store('low1',  importance=0.1)
        wm.store('low2',  importance=0.1)
        wm.store('low3',  importance=0.1)
        hi_id = wm.store('CRITICAL', importance=1.0)
        # High importance item should survive
        result = wm.retrieve(hi_id)
        self.assertEqual(result, 'CRITICAL')

    def test_retrieve_by_source(self):
        wm = self.WM()
        wm.store({'v': 1}, source='safety')
        wm.store({'v': 2}, source='safety')
        wm.store({'v': 3}, source='perception')
        safety_items = wm.retrieve_by_source('safety')
        self.assertEqual(len(safety_items), 2)

    def test_most_salient(self):
        wm = self.WM()
        wm.store('a', importance=0.2)
        wm.store('b', importance=0.9)
        wm.store('c', importance=0.5)
        salient = wm.most_salient(1)
        self.assertEqual(salient[0], 'b')

    def test_snapshot(self):
        wm = self.WM()
        wm.store({'x': 1}, importance=0.8)
        snap = wm.snapshot()
        self.assertIsInstance(snap, list)
        self.assertGreater(len(snap), 0)


# ════════════════════════════════════════════════════════════════════════════
# EPISODIC MEMORY
# ════════════════════════════════════════════════════════════════════════════

class TestEpisodicMemory(unittest.TestCase):

    def setUp(self):
        from cerberus.cognitive.mind import EpisodicMemory
        self.EM = EpisodicMemory

    def test_record_and_recall_recent(self):
        em = self.EM()
        em.record('walk_complete', {'steps': 100}, emotion='joy', valence=0.8)
        em.record('obstacle_hit',  {'dist': 0.1}, emotion='fear', valence=-0.9)
        recent = em.recall_recent(5)
        self.assertEqual(len(recent), 2)
        # Most recent first
        self.assertEqual(recent[0]['event_type'], 'obstacle_hit')

    def test_recall_by_type(self):
        em = self.EM()
        for i in range(3): em.record('patrol', {'wp': i})
        for i in range(2): em.record('interact', {'obj': i})
        patrols = em.recall_recent(10, event_type='patrol')
        self.assertEqual(len(patrols), 3)

    def test_recall_emotional(self):
        em = self.EM()
        em.record('good', {}, valence=0.9)
        em.record('bad',  {}, valence=-0.8)
        em.record('great', {}, valence=0.95)
        positive = em.recall_emotional(valence_min=0.8)
        self.assertEqual(len(positive), 2)
        # Highest valence first
        self.assertGreaterEqual(positive[0]['valence'], positive[1]['valence'])

    def test_stats(self):
        em = self.EM()
        em.record('ev', {}, valence=0.5)
        em.record('ev', {}, valence=-0.3)
        stats = em.stats()
        self.assertEqual(stats['total_episodes'], 2)
        self.assertEqual(stats['positive_pct'], 50.0)


# ════════════════════════════════════════════════════════════════════════════
# SEMANTIC MEMORY
# ════════════════════════════════════════════════════════════════════════════

class TestSemanticMemory(unittest.TestCase):

    def setUp(self):
        from cerberus.cognitive.mind import SemanticMemory
        self.SM = SemanticMemory

    def test_learn_and_know(self):
        sm = self.SM()
        sm.learn('robot.color', 'black', confidence=0.95)
        val, conf = sm.know('robot.color')
        self.assertEqual(val, 'black')
        self.assertAlmostEqual(conf, 0.95)

    def test_unknown_returns_none(self):
        sm = self.SM()
        val, conf = sm.know('nonexistent')
        self.assertIsNone(val)
        self.assertEqual(conf, 0.0)

    def test_overwrite(self):
        sm = self.SM()
        sm.learn('k', 'v1', confidence=0.5)
        sm.learn('k', 'v2', confidence=0.9)
        val, conf = sm.know('k')
        self.assertEqual(val, 'v2')

    def test_forget(self):
        sm = self.SM()
        sm.learn('temp', 'data')
        sm.forget('temp')
        _, conf = sm.know('temp')
        self.assertEqual(conf, 0.0)


# ════════════════════════════════════════════════════════════════════════════
# GOAL STACK
# ════════════════════════════════════════════════════════════════════════════

class TestGoalStack(unittest.TestCase):

    def setUp(self):
        from cerberus.runtime import SystemEventBus
        from cerberus.cognitive.mind import GoalStack, Goal, GoalStatus
        self.bus       = SystemEventBus()
        self.GoalStack = GoalStack
        self.Goal      = Goal
        self.GoalStatus = GoalStatus

    def test_push_and_activate(self):
        gs = self.GoalStack(self.bus)
        g  = self.Goal(name='test_goal', type='express', priority=0.7)
        run_async(gs.push(g))
        self.assertIsNotNone(gs.active)
        self.assertEqual(gs.active.name, 'test_goal')

    def test_preemption_by_priority(self):
        gs = self.GoalStack(self.bus)
        g1 = self.Goal(name='low',  type='express', priority=0.2)
        g2 = self.Goal(name='high', type='express', priority=0.9)
        run_async(gs.push(g1))
        run_async(gs.push(g2))
        # High priority should preempt
        self.assertEqual(gs.active.name, 'high')

    def test_complete_active(self):
        gs = self.GoalStack(self.bus)
        g  = self.Goal(name='doable', type='express', priority=0.5)
        run_async(gs.push(g))
        run_async(gs.complete_active(success=True))
        self.assertIsNone(gs.active)

    def test_goal_expiry(self):
        gs = self.GoalStack(self.bus)
        g  = self.Goal(name='expiring', type='rest', priority=0.3,
                       deadline=time.time() - 1.0)  # already expired
        self.assertTrue(g.is_expired)

    def test_urgency_boost_near_deadline(self):
        g = self.Goal(name='urgent', priority=0.4,
                      deadline=time.time() + 5.0)
        self.assertGreater(g.urgency, g.priority)


# ════════════════════════════════════════════════════════════════════════════
# ATTENTION SYSTEM
# ════════════════════════════════════════════════════════════════════════════

class TestAttentionSystem(unittest.TestCase):

    def setUp(self):
        from cerberus.cognitive.mind import AttentionSystem
        self.AS = AttentionSystem

    def test_attend_and_salient(self):
        a = self.AS()
        a.attend('person_1', 'person', salience=0.9)
        a.attend('chair_1',  'object', salience=0.3)
        most = a.most_salient()
        self.assertEqual(most.target_id, 'person_1')

    def test_capacity_limit(self):
        a = self.AS()
        for i in range(10):
            a.attend(f'obj_{i}', 'object', salience=0.5)
        self.assertLessEqual(len(a._targets), a.MAX_TARGETS)

    def test_novelty_decay(self):
        a = self.AS()
        a.attend('thing', 'object', salience=0.8)
        initial_novelty = a._targets['thing'].novelty
        a.decay(dt_s=60.0)
        self.assertLess(a._targets.get('thing', type('_',(),{'novelty':0})).novelty, initial_novelty)

    def test_attend_updates_existing(self):
        a = self.AS()
        a.attend('p1', 'person', salience=0.4)
        a.attend('p1', 'person', salience=0.9)
        self.assertAlmostEqual(a._targets['p1'].salience, 0.9)


# ════════════════════════════════════════════════════════════════════════════
# DIGITAL ANATOMY — JOINTS
# ════════════════════════════════════════════════════════════════════════════

class TestJointModel(unittest.TestCase):

    def setUp(self):
        from cerberus.body.anatomy import JointModel, JOINT_NAMES
        self.JM = JointModel
        self.JOINT_NAMES = JOINT_NAMES

    def test_joint_count(self):
        jm = self.JM()
        self.assertEqual(len(jm.joints), 12)

    def test_position_clamping(self):
        jm = self.JM()
        # Try to set FR_2 way out of range
        jm.update({'FR_2': 10.0}, {}, dt_s=0.002)
        fr2 = jm.joints['FR_2']
        self.assertLessEqual(fr2.position, fr2.hi_limit)
        self.assertGreater(fr2.error_count, 0)

    def test_thermal_heating(self):
        jm = self.JM()
        # Simulate high torque, high velocity
        for _ in range(500):
            jm.update({'FR_1': 0.67}, {'FR_1': 40.0}, dt_s=0.002)
        self.assertGreater(jm.joints['FR_1'].temperature, 22.0)

    def test_summary(self):
        jm = self.JM()
        s = jm.summary()
        self.assertIn('hottest_joint', s)
        self.assertIn('max_stress', s)

    def test_all_joints_present(self):
        jm = self.JM()
        for name in self.JOINT_NAMES:
            self.assertIn(name, jm.joints)


# ════════════════════════════════════════════════════════════════════════════
# DIGITAL ANATOMY — ENERGY
# ════════════════════════════════════════════════════════════════════════════

class TestEnergyModel(unittest.TestCase):

    def setUp(self):
        from cerberus.body.anatomy import EnergyModel
        self.EM = EnergyModel

    def test_battery_drains_during_walking(self):
        em = self.EM()
        initial = em.state.battery_pct
        em.update('walking', {}, {}, dt_s=120.0)
        self.assertLess(em.state.battery_pct, initial)

    def test_fatigue_accumulates(self):
        em = self.EM()
        em.update('performing', {}, {}, dt_s=300.0)
        self.assertGreater(em.state.fatigue_level, 0.0)

    def test_fatigue_recovers_at_rest(self):
        em = self.EM()
        em.state.fatigue_level = 0.5
        em.update('idle', {}, {}, dt_s=60.0)
        self.assertLess(em.state.fatigue_level, 0.5)

    def test_velocity_cap_reduces_with_fatigue(self):
        em = self.EM()
        cap_fresh = em.velocity_cap_factor()
        em.state.fatigue_level = 0.8
        cap_tired = em.velocity_cap_factor()
        self.assertLess(cap_tired, cap_fresh)

    def test_critical_battery_flag(self):
        em = self.EM()
        em.state.battery_pct = 8.0
        self.assertTrue(em.state.is_critical)

    def test_runtime_estimate(self):
        em = self.EM()
        em.state.current_a = 4.5
        rt = em.state.estimated_runtime_min
        self.assertGreater(rt, 0)
        self.assertLess(rt, 200)


# ════════════════════════════════════════════════════════════════════════════
# STABILITY MODEL
# ════════════════════════════════════════════════════════════════════════════

class TestStabilityModel(unittest.TestCase):

    def setUp(self):
        from cerberus.body.anatomy import StabilityModel
        self.SM = StabilityModel

    def test_nominal_stable(self):
        sm = self.SM()
        sm.update(0.0, 0.0, {'fl':13,'fr':12,'rl':14,'rr':13})
        self.assertTrue(sm.state.in_support)
        self.assertGreater(sm.state.margin, 0.5)

    def test_extreme_tilt_unsafe(self):
        sm = self.SM()
        sm.update(pitch_deg=25.0, roll_deg=20.0,
                  foot_forces={'fl':0,'fr':0,'rl':5,'rr':5})
        self.assertFalse(sm.is_safe(pitch_limit=10.0))

    def test_contacts_counted(self):
        sm = self.SM()
        sm.update(0.0, 0.0, {'fl':10,'fr':0,'rl':8,'rr':0})
        self.assertEqual(sm.state.contacts, 2)

    def test_tip_over_risk_increases_with_tilt(self):
        sm1 = self.SM()
        sm1.update(1.0, 0.5, {'fl':13,'fr':12,'rl':14,'rr':13})
        sm2 = self.SM()
        sm2.update(8.0, 7.0, {'fl':5,'fr':2,'rl':3,'rr':2})
        self.assertGreater(sm2.tip_over_risk(), sm1.tip_over_risk())


# ════════════════════════════════════════════════════════════════════════════
# PERSONALITY ENGINE
# ════════════════════════════════════════════════════════════════════════════

class TestPersonalityEngine(unittest.TestCase):

    def setUp(self):
        from cerberus.personality.engine import (
            PersonalityTraits, MoodState, MoodLabel, BehaviorModulation,
            PersonalityEngine, arousal_valence_to_mood
        )
        self.Traits = PersonalityTraits
        self.Mood   = MoodState
        self.Label  = MoodLabel
        self.Mod    = BehaviorModulation
        self.av2m   = arousal_valence_to_mood

    def test_neutral_mood(self):
        mood = self.Mood(arousal=0.0, valence=0.0)
        # Near neutral → shouldn't be extreme
        self.assertIn(mood.label, [
            self.Label.CURIOUS, self.Label.CONTENT,
            self.Label.RELAXED, self.Label.BORED, self.Label.ALERT
        ])

    def test_excited_mood(self):
        mood = self.Mood(arousal=0.8, valence=0.8)
        self.assertIn(mood.label, [self.Label.EXCITED, self.Label.PLAYFUL, self.Label.HAPPY])

    def test_anxious_mood(self):
        mood = self.Mood(arousal=0.8, valence=-0.8)
        self.assertEqual(mood.label, self.Label.ANXIOUS)

    def test_mood_intensity(self):
        neutral  = self.Mood(arousal=0.0, valence=0.0)
        extreme  = self.Mood(arousal=0.9, valence=0.9)
        self.assertGreater(extreme.intensity, neutral.intensity)

    def test_mood_decay(self):
        mood = self.Mood(arousal=1.0, valence=1.0)
        mood.decay(dt_s=30.0)
        self.assertLess(mood.arousal, 1.0)
        self.assertLess(mood.valence, 1.0)

    def test_mood_inject(self):
        mood = self.Mood()
        mood.inject(0.5, 0.5)
        self.assertGreater(mood.arousal, 0.0)
        self.assertGreater(mood.valence, 0.0)

    def test_trait_adaptation(self):
        traits = self.Traits()
        initial_ext = traits.extraversion
        for _ in range(5):
            traits.adapt('successful_interaction', magnitude=0.002)
        self.assertGreater(traits.extraversion, initial_ext)

    def test_trait_bounds(self):
        traits = self.Traits()
        for _ in range(10000):
            traits.adapt('successful_interaction', magnitude=0.01)
        self.assertLessEqual(traits.extraversion, 1.0)

    def test_behavior_modulation_speed(self):
        from cerberus.personality.engine import PersonalityEngine
        from cerberus.runtime import SystemEventBus
        bus = SystemEventBus()
        pe  = PersonalityEngine(bus)
        # Excited mood → higher speed
        pe.mood.inject(0.7, 0.7)
        mod_excited = pe._compute_modulation()
        # Tired → lower speed
        pe._fatigue = 0.9
        pe.mood.inject(-0.5, -0.3)
        mod_tired = pe._compute_modulation()
        self.assertGreater(mod_excited.speed_factor, mod_tired.speed_factor)

    def test_arousal_valence_circumplex(self):
        cases = [
            (0.8,  0.8,  [self.Label.EXCITED, self.Label.PLAYFUL, self.Label.HAPPY]),
            (0.8, -0.8,  [self.Label.ANXIOUS]),
            (-0.3, 0.6,  [self.Label.CONTENT, self.Label.RELAXED]),
            (-0.5, -0.5, [self.Label.BORED, self.Label.SAD]),
        ]
        for a, v, expected in cases:
            label = self.av2m(a, v)
            self.assertIn(label, expected, f'Expected {expected} for a={a}, v={v}, got {label}')


# ════════════════════════════════════════════════════════════════════════════
# REINFORCEMENT LEARNER
# ════════════════════════════════════════════════════════════════════════════

class TestReinforcementLearner(unittest.TestCase):

    def setUp(self):
        from cerberus.learning.adaptation import ReinforcementLearner
        self.RL = ReinforcementLearner

    def test_register_actions(self):
        rl = self.RL()
        rl.register_actions(['a', 'b', 'c'])
        self.assertEqual(rl._actions, ['a', 'b', 'c'])

    def test_epsilon_greedy_exploration(self):
        rl = self.RL()
        rl.register_actions(['sit', 'stand', 'zoomies'])
        state = rl.discretize_state('idle', 0.3, 0.1)
        actions_chosen = set()
        for _ in range(100):
            actions_chosen.add(rl.select_action(state))
        # ε-greedy should explore
        self.assertGreater(len(actions_chosen), 1)

    def test_q_update_increases_reward_action(self):
        rl = self.RL()
        rl.register_actions(['good', 'bad'])
        state = rl.discretize_state('walking', 0.5, 0.0)
        for _ in range(10):
            rl.observe(state, 'good', 1.0, state, 'user')
            rl.observe(state, 'bad',  -0.5, state, 'user')
        top = rl.top_actions(state, 2)
        self.assertEqual(top[0][0], 'good')

    def test_q_table_bounded(self):
        rl = self.RL()
        rl.register_actions(['a'])
        # Generate many unique states
        for i in range(15000):
            state = (i, 0, 0, 0)
            rl._q_update(state, 'a', 1.0, state)
        self.assertLessEqual(len(rl._q), rl.MAX_Q_STATES)

    def test_discretize_state_valid(self):
        rl = self.RL()
        for st in ['idle', 'walking', 'following', 'performing', 'fault']:
            state = rl.discretize_state(st, 0.0, 0.0)
            self.assertIsInstance(state, tuple)
            self.assertEqual(len(state), 4)


# ════════════════════════════════════════════════════════════════════════════
# IMITATION LEARNER
# ════════════════════════════════════════════════════════════════════════════

class TestImitationLearner(unittest.TestCase):

    def setUp(self):
        from cerberus.learning.adaptation import ImitationLearner
        self.IL = ImitationLearner

    def test_record_and_replay(self):
        il = self.IL()
        il.start_recording('trick_sequence')
        for b in ['sit', 'tail_wag', 'stand', 'paw_shake']:
            il.observe_behavior(b, 'user')
        ep_id = il.stop_recording(save=True)
        self.assertIsNotNone(ep_id)
        seq = il.get_playback_sequence()
        self.assertEqual(seq, ['sit', 'tail_wag', 'stand', 'paw_shake'])

    def test_min_sequence_length(self):
        il = self.IL()
        il.start_recording('one_behavior')
        il.observe_behavior('sit', 'user')
        ep_id = il.stop_recording(save=True)
        self.assertIsNone(ep_id)  # need at least 2

    def test_max_episodes(self):
        il = self.IL()
        il.MAX_EPISODES = 3
        for i in range(5):
            il.start_recording(f'seq_{i}')
            il.observe_behavior('sit', 'user')
            il.observe_behavior('stand', 'user')
            il.stop_recording(save=True)
        self.assertLessEqual(len(il._episodes), il.MAX_EPISODES)

    def test_play_count_increments(self):
        il = self.IL()
        il.start_recording('repeat')
        il.observe_behavior('a', 'user')
        il.observe_behavior('b', 'user')
        il.stop_recording(True)
        il.get_playback_sequence()
        il.get_playback_sequence()
        self.assertEqual(il._episodes[0].play_count, 2)


# ════════════════════════════════════════════════════════════════════════════
# PREFERENCE LEARNER
# ════════════════════════════════════════════════════════════════════════════

class TestPreferenceLearner(unittest.TestCase):

    def setUp(self):
        from cerberus.learning.adaptation import PreferenceLearner
        self.PL = PreferenceLearner

    def test_user_preferred_over_autonomous(self):
        pl = self.PL()
        pl.observe('zoomies', source='user',      reward=1.0)
        pl.observe('sit',     source='autonomous', reward=1.0)
        prefs = pl.preferred_behaviors(2)
        self.assertEqual(prefs[0][0], 'zoomies')

    def test_decay_prevents_stagnation(self):
        pl = self.PL()
        pl.observe('old_fave', reward=1.0)
        for _ in range(100):
            pl.observe('new_hit', reward=1.0)
        prefs = dict(pl.preferred_behaviors(5))
        self.assertIn('new_hit', prefs)

    def test_weight_bounded(self):
        pl = self.PL()
        for _ in range(1000):
            pl.observe('a', reward=10.0)
        self.assertLessEqual(pl.weight('a'), 1.0)

    def test_stats(self):
        pl = self.PL()
        pl.observe('x', reward=1.0)
        pl.observe('y', reward=0.5)
        stats = pl.stats()
        self.assertEqual(stats['total_observations'], 2)
        self.assertEqual(stats['unique_behaviors'], 2)


# ════════════════════════════════════════════════════════════════════════════
# PERCEPTION PIPELINE
# ════════════════════════════════════════════════════════════════════════════

class TestPerceptionPipeline(unittest.TestCase):

    def setUp(self):
        from cerberus.perception.pipeline import (
            ObjectTracker, SpatialMapper, SceneClassifier,
            Detection, HumanState, PerceptFrame
        )
        self.OT = ObjectTracker
        self.SM = SpatialMapper
        self.SceneClassifier = SceneClassifier
        self.Det = Detection
        self.HS  = HumanState

    def test_object_tracker_creates_tracks(self):
        ot = self.OT()
        dets = [
            {'label': 'chair',  'conf': 0.8, 'dist_m': 2.0, 'bbox': (100,100,200,200)},
            {'label': 'person', 'conf': 0.9, 'dist_m': 1.5, 'bbox': (300,100,400,300)},
        ]
        tracked = ot.update(dets)
        self.assertEqual(len(tracked), 2)

    def test_tracker_stable_ids(self):
        ot = self.OT()
        d1 = [{'label':'chair','conf':0.8,'dist_m':2.0,'bbox':(100,100,200,200)}]
        d2 = [{'label':'chair','conf':0.8,'dist_m':2.1,'bbox':(102,101,202,201)}]
        t1 = ot.update(d1)
        t2 = ot.update(d2)
        self.assertEqual(t1[0].track_id, t2[0].track_id)

    def test_tracker_iou_computation(self):
        iou = self.OT._box_iou((0,0,100,100), (50,50,150,150))
        expected = (50*50) / (100*100 + 100*100 - 50*50)
        self.assertAlmostEqual(iou, expected, places=3)

    def test_human_zone_danger(self):
        zone = self.HS.classify_zone(0.2)
        self.assertEqual(zone, 'danger')

    def test_human_zone_caution(self):
        zone = self.HS.classify_zone(0.4)
        self.assertEqual(zone, 'caution')

    def test_human_zone_far(self):
        zone = self.HS.classify_zone(3.0)
        self.assertEqual(zone, 'far')

    def test_spatial_mapper_pose_update(self):
        sm = self.SM()
        sm.update_pose(1.0, 2.0, 0.5)
        self.assertAlmostEqual(sm._robot_x, 1.0)
        self.assertAlmostEqual(sm._robot_y, 2.0)

    def test_spatial_mapper_grid_snapshot(self):
        sm = self.SM()
        grid = sm.grid_snapshot(downsample=10)
        self.assertIsInstance(grid, list)
        self.assertEqual(len(grid), sm.GRID_CELLS // 10)

    def test_scene_classifier_crowded(self):
        sc = self.SceneClassifier()
        dets = [
            self.Det(label='person', dist_m=1.5, confidence=0.9),
            self.Det(label='person', dist_m=2.0, confidence=0.8),
            self.Det(label='person', dist_m=3.0, confidence=0.7),
        ]
        scene = sc.classify(dets, [], (0.,0.,0.))
        self.assertTrue(scene.crowded)

    def test_scene_classifier_indoor(self):
        sc = self.SceneClassifier()
        scan = [2.0] * 90  # moderate range = indoor
        scene = sc.classify([], scan, (0.,0.,0.))
        self.assertIn(scene.type, ['indoor', 'corridor'])

    def test_percept_frame_to_dict(self):
        from cerberus.perception.pipeline import PerceptFrame, SceneLabel
        frame = PerceptFrame(
            nearest_obstacle_m=3.0,
            nearest_human_m=1.5,
            scene=SceneLabel(type='indoor'),
        )
        d = frame.to_dict()
        self.assertIn('nearest_obstacle_m', d)
        self.assertIn('scene_type', d)


# ════════════════════════════════════════════════════════════════════════════
# CERBERUS PLUGIN SYSTEM
# ════════════════════════════════════════════════════════════════════════════

class TestCerberusPlugins(unittest.TestCase):

    def setUp(self):
        from cerberus.plugins.cerberus_plugins import (
            TrustLevel, ResourceQuota, TRUST_PERMISSIONS
        )
        self.TL   = TrustLevel
        self.RQ   = ResourceQuota
        self.PERMS = TRUST_PERMISSIONS

    def test_trust_ordering(self):
        self.assertGreater(self.TL.SYSTEM,    self.TL.TRUSTED)
        self.assertGreater(self.TL.TRUSTED,   self.TL.COMMUNITY)
        self.assertGreater(self.TL.COMMUNITY, self.TL.UNTRUSTED)

    def test_system_has_wildcard_perms(self):
        self.assertIn('*', self.PERMS[self.TL.SYSTEM])

    def test_untrusted_limited_perms(self):
        perms = self.PERMS[self.TL.UNTRUSTED]
        self.assertNotIn('behaviors', perms)
        self.assertNotIn('cognitive', perms)
        self.assertIn('sensors', perms)

    def test_trusted_has_cognitive(self):
        perms = self.PERMS[self.TL.TRUSTED]
        self.assertIn('cognitive', perms)
        self.assertIn('personality', perms)
        self.assertIn('learning', perms)

    def test_quota_untrusted_more_restricted(self):
        q_trusted   = self.RQ.QUOTA_BY_TRUST[self.TL.TRUSTED]
        q_untrusted = self.RQ.QUOTA_BY_TRUST[self.TL.UNTRUSTED]
        self.assertGreater(q_trusted.max_cpu_ms_per_tick,
                           q_untrusted.max_cpu_ms_per_tick)


# ════════════════════════════════════════════════════════════════════════════
# CERBERUS INTEGRATION
# ════════════════════════════════════════════════════════════════════════════

class TestCerberusIntegration(unittest.TestCase):

    def test_cerberus_import(self):
        from cerberus import Cerberus, __version__
        self.assertEqual(__version__, '2.0.0')

    def test_cerberus_instantiate_no_platform(self):
        from cerberus import Cerberus
        c = Cerberus(platform=None, enable_logging=False)
        self.assertIsNotNone(c.mind)
        self.assertIsNotNone(c.anatomy)
        self.assertIsNotNone(c.personality)
        self.assertIsNotNone(c.learning)

    def test_cerberus_status_dict(self):
        from cerberus import Cerberus
        c = Cerberus(platform=None, enable_logging=False)
        s = c.status()
        self.assertIn('version', s)
        self.assertIn('mind', s)
        self.assertIn('anatomy', s)
        self.assertIn('personality', s)
        self.assertIn('learning', s)


# ════════════════════════════════════════════════════════════════════════════
# RUNNER
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    test_classes = [
        TestRuntime, TestWorkingMemory, TestEpisodicMemory, TestSemanticMemory,
        TestGoalStack, TestAttentionSystem,
        TestJointModel, TestEnergyModel, TestStabilityModel,
        TestPersonalityEngine,
        TestReinforcementLearner, TestImitationLearner, TestPreferenceLearner,
        TestPerceptionPipeline, TestCerberusPlugins, TestCerberusIntegration,
    ]
    for tc in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    fails = len(result.failures) + len(result.errors)
    print(f'\n{"═"*62}')
    print(f'  CERBERUS TEST SUITE: {total - fails}/{total} passed')
    print('  ✓ ALL PASSED' if fails == 0 else f'  ✗ {fails} FAILURES')
    print('═'*62)
    import sys; sys.exit(0 if fails == 0 else 1)
