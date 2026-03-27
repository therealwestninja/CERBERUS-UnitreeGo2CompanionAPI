"""
cerberus/cli/cerberus_cli.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Developer CLI — Extended Developer Toolkit

Extends the Go2 CLI with CERBERUS-specific commands:

  cerberus status           Full CERBERUS system status
  cerberus mind             Cognitive state (memory, goals, attention)
  cerberus mind memory      Working memory contents
  cerberus mind goal push   Push a goal
  cerberus mind goal done   Complete active goal
  cerberus body             Digital anatomy state
  cerberus body joints      Per-joint health
  cerberus body energy      Battery + fatigue
  cerberus personality      Mood + traits
  cerberus personality mood  Set mood via event
  cerberus learning         Learning status
  cerberus learning prefer  Record preference
  cerberus learning suggest  Get behavior suggestion
  cerberus learning reset   Reset all learning
  cerberus perception       Latest perception frame
  cerberus scenario list    List available test scenarios
  cerberus scenario run     Run a scenario
  cerberus demo             Run autonomous behavior demo
  cerberus plugin list      List CERBERUS plugins
  cerberus plugin new       Scaffold a new plugin
"""

import argparse
import json
import os
import sys
import textwrap
import time
from typing import Optional
from urllib import request, error

API_URL = os.getenv('GO2_API_URL', 'http://localhost:8080')
TOKEN   = os.getenv('GO2_TOKEN', os.getenv('GO2_API_TOKEN', ''))

_COLOR = sys.platform != 'win32' or os.getenv('FORCE_COLOR')
def _c(code, t): return f'\033[{code}m{t}\033[0m' if _COLOR else t
GREEN  = lambda t: _c('32', t)
RED    = lambda t: _c('31', t)
YELLOW = lambda t: _c('33', t)
CYAN   = lambda t: _c('36', t)
BOLD   = lambda t: _c('1',  t)
DIM    = lambda t: _c('2',  t)
MAGENTA = lambda t: _c('35', t)

def ok(m):   print(GREEN('✓') + ' ' + m)
def err(m):  print(RED('✗') + ' ' + m, file=sys.stderr)
def warn(m): print(YELLOW('⚠') + ' ' + m)
def info(m): print(CYAN('ℹ') + ' ' + m)
def section(t): print('\n' + BOLD(t))
def row(k, v, ok_val=None):
    vs = GREEN(str(v)) if ok_val is True else RED(str(v)) if ok_val is False else str(v)
    print(f'  {DIM(k+":"): <28} {vs}')

def _req(method, path, body=None, timeout=8.0):
    url = f'{API_URL}{path}'
    data = json.dumps(body).encode() if body else None
    headers = {'Content-Type': 'application/json'}
    if TOKEN: headers['Authorization'] = f'Bearer {TOKEN}'
    try:
        req = request.Request(url, data=data, headers=headers, method=method)
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except error.HTTPError as e:
        try: detail = json.loads(e.read()).get('detail', str(e))
        except: detail = str(e)
        raise RuntimeError(f'HTTP {e.code}: {detail}')
    except error.URLError as e:
        raise RuntimeError(f'Connection failed to {API_URL}: {e.reason}')

def GET(p):         return _req('GET', p)
def POST(p, b={}):  return _req('POST', p, b)
def DELETE(p):      return _req('DELETE', p)
def PATCH(p, b):    return _req('PATCH', p, b)


# ════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════════════════

def cmd_status(args):
    try:
        s = GET('/api/v1/cerberus/status')
        section(f'CERBERUS {s.get("version","?")} Status')

        rt = s.get('runtime', {})
        row('Started',    'Yes' if rt.get('started') else 'No', ok_val=rt.get('started'))
        row('Uptime',     f'{rt.get("uptime_s",0):.1f}s')
        row('Subsystems', str(len(rt.get('subsystems', []))))

        # Watchdogs
        wds = rt.get('watchdogs', {})
        section('Watchdogs')
        for name, wd in wds.items():
            st = wd.get('healthy', True)
            sym = GREEN('●') if st else RED('●')
            age = wd.get('age_s', 0)
            print(f'  {sym} {name:<30} {DIM(f"age={age:.1f}s")}')

        # Scheduler stats
        sched = rt.get('scheduler', {})
        if sched:
            section('Scheduler (tick rates)')
            for prio, stats in sched.items():
                target = stats.get('target_hz', 0)
                actual = stats.get('achieved_hz', 0)
                overruns = stats.get('overruns', 0)
                ok_rate = actual >= target * 0.8
                row(prio, f'{actual:.1f}Hz / {target:.0f}Hz ({overruns} overruns)', ok_val=ok_rate)

    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_mind(args):
    try:
        m = GET('/api/v1/cerberus/mind')
        section('Cognitive Mind')
        gs = m.get('goal_stack', {})
        active = gs.get('active')
        row('Active goal', active['name'] if active else 'none',
            ok_val=bool(active))
        row('Pending goals', str(len(gs.get('pending', []))))
        row('Episodic episodes',
            str(m.get('episodic_stats', {}).get('total_episodes', 0)))
        row('Working memory items',
            str(len(m.get('working_memory', []))))
        row('Semantic facts', str(m.get('semantic_facts', 0)))
        att = m.get('attention', {})
        row('Attention focus',
            att.get('focused_on') or 'nothing',
            ok_val=bool(att.get('focused_on')))

        if args.verbose:
            section('Working Memory')
            for item in m.get('working_memory', [])[:5]:
                print(f'  [{item["source"]}] {item["content_type"]} '
                      f'(importance={item["importance"]:.2f}, age={item["age_s"]:.1f}s)')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_goal_push(args):
    body = {'name': args.name, 'type': args.type, 'priority': args.priority}
    if args.deadline:
        body['deadline_s'] = args.deadline
    try:
        r = POST('/api/v1/cerberus/mind/goals', body)
        ok(f'Goal pushed: {args.name} [{args.type}] (id={r.get("goal_id")})')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_goal_done(args):
    try:
        r = DELETE(f'/api/v1/cerberus/mind/goals/active?success={str(args.success).lower()}')
        ok(f'Active goal marked {"complete" if args.success else "failed"}')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_body(args):
    try:
        b = GET('/api/v1/cerberus/body')
        section('Digital Anatomy')
        e = b.get('energy', {})
        row('Battery',    f'{e.get("battery_pct",0):.0f}%',
            ok_val=(e.get('battery_pct',100) > 25))
        row('Fatigue',    b.get('fatigue','?'),
            ok_val=(b.get('fatigue') in ('fresh', 'mild')))
        row('Velocity cap', f'{b.get("velocity_cap",1):.2f}×')
        row('Power draw', f'{e.get("power_w",0):.1f}W')
        s = b.get('stability', {})
        row('Stability',  f'{s.get("margin",0):.3f}',
            ok_val=(s.get('margin',1.0) > 0.3))
        j = b.get('joints', {})
        row('Max temp',   f'{j.get("max_temp_c","?")}°C',
            ok_val=(j.get('max_temp_c', 30) < 65))
        if args.verbose:
            section('Per-Joint State')
            joints = GET('/api/v1/cerberus/body/joints')
            for jt in joints.get('joints', []):
                temp_ok = jt['temp_c'] < 60
                sym = GREEN('○') if temp_ok else YELLOW('○')
                print(f'  {sym} {jt["name"]:<8} '
                      f't={jt["temp_c"]}°C  τ={jt["torque"]:.1f}Nm  '
                      f'stress={jt["stress"]:.3f}'
                      + (' [LIMIT]' if jt['at_limit'] else ''))
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_personality(args):
    try:
        p = GET('/api/v1/cerberus/personality')
        mood   = p.get('mood', {})
        traits = p.get('traits', {})
        mod    = p.get('modulation', {})
        section('Personality Engine')
        row('Mood label',   mood.get('label','?'))
        row('Arousal',      f'{mood.get("arousal",0):+.3f}')
        row('Valence',      f'{mood.get("valence",0):+.3f}',
            ok_val=(mood.get('valence', 0) > 0))
        row('Intensity',    f'{mood.get("intensity",0):.3f}')
        section('Traits')
        for tr in ('openness','conscientiousness','extraversion','agreeableness','neuroticism'):
            val = traits.get(tr, 0)
            bar = '█' * int(val * 15) + '░' * (15 - int(val * 15))
            print(f'  {tr:<18} {bar} {val:.2f}')
        section('Behavior Modulation')
        row('Speed factor',       f'{mod.get("speed_factor",1):.2f}×')
        row('Expressiveness',     f'{mod.get("expressiveness",1):.2f}×')
        row('Approach willingness', f'{mod.get("approach_willingness",1):.2f}')
        row('Rest drive',         f'{mod.get("rest_drive",0):.2f}')
        row('Exploration drive',  f'{mod.get("exploration_drive",0):.2f}')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_mood_event(args):
    try:
        r = POST('/api/v1/cerberus/personality/event',
                 {'event': args.event, 'magnitude': args.magnitude})
        mood = r.get('mood', {})
        ok(f'Mood event injected: {args.event} × {args.magnitude}')
        info(f'New mood: {mood.get("label","?")} '
             f'(a={mood.get("arousal",0):+.2f}, v={mood.get("valence",0):+.2f})')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_learning(args):
    try:
        l = GET('/api/v1/cerberus/learning')
        rl = l.get('rl', {})
        section('Learning System')
        row('Q-table states',  str(rl.get('q_states', 0)))
        row('Q-table updates', str(rl.get('updates', 0)))
        row('Buffer size',     str(rl.get('buffer', {}).get('count', 0)))
        row('Avg reward',      f'{rl.get("buffer",{}).get("avg_reward",0):.3f}')
        row('Suggestion',      l.get('suggestion','?'))
        row('Imitation eps',   str(l.get('imitation_episodes', 0)))
        prefs = l.get('preferences', {}).get('top_preferences', [])
        if prefs:
            section('Top Preferences')
            for beh, weight in prefs[:5]:
                bar = '█' * int(weight * 50)
                print(f'  {beh:<20} {bar} {weight:.3f}')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_prefer(args):
    try:
        r = POST('/api/v1/cerberus/learning/prefer',
                 {'behavior_id': args.behavior, 'reward': args.reward})
        ok(f'Preference recorded: {args.behavior} (reward={args.reward}, '
           f'weight={r.get("weight",0):.4f})')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_suggest(args):
    try:
        r = GET('/api/v1/cerberus/learning/suggest')
        ok(f'Suggested behavior: {BOLD(r.get("suggestion","?"))}')
        prefs = r.get('top_prefs', [])
        if prefs:
            print(f'  Top preferences:')
            for p in prefs[:3]:
                print(f'    {p["behavior"]:<20} weight={p["weight"]:.4f}')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_learning_reset(args):
    if not args.yes:
        confirm = input('Reset ALL learning data? This cannot be undone. [y/N] ')
        if confirm.lower() != 'y':
            info('Aborted'); return
    try:
        DELETE('/api/v1/cerberus/learning/reset')
        ok('All learning data reset')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_perception(args):
    try:
        p = GET('/api/v1/cerberus/perception')
        section('Perception Frame')
        row('Objects detected',    str(p.get('object_count', 0)))
        row('Humans detected',     str(p.get('human_count', 0)),
            ok_val=(p.get('human_count', 0) == 0))
        row('Nearest obstacle',    f'{p.get("nearest_obstacle_m", 99):.2f}m',
            ok_val=(p.get('nearest_obstacle_m', 99) > 1.0))
        row('Human in zone',       str(p.get('human_in_danger_zone', False)),
            ok_val=(not p.get('human_in_danger_zone', False)))
        row('Scene type',          p.get('scene_type', '?'))
        if args.verbose:
            section('Detections')
            for d in p.get('detections', []):
                print(f'  [{d.get("track_id")}] {d.get("label","?"):<12} '
                      f'conf={d.get("conf",0):.2f}  '
                      f'dist={d.get("dist_m",0):.2f}m  '
                      f'angle={d.get("angle_deg",0):.0f}°')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


def cmd_demo(args):
    """Run an autonomous behavior demonstration sequence."""
    info(f'Starting CERBERUS autonomous demo: {args.scenario}')
    scenarios = {
        'companion': [
            ('ARM', {},              'Arming system'),
            ('STAND', {},            'Standing up'),
            ('RUN_BEHAVIOR', {'behavior_id': 'idle_breath'}, 'Breathing idle'),
            ('RUN_BEHAVIOR', {'behavior_id': 'head_tilt'},   'Curious head tilt'),
            ('RUN_BEHAVIOR', {'behavior_id': 'tail_wag'},    'Happy wag'),
            ('RUN_BEHAVIOR', {'behavior_id': 'play_bow'},    'Play bow'),
            ('RUN_BEHAVIOR', {'behavior_id': 'zoomies'},     'Zoomies!'),
            ('SIT', {},              'Sitting down'),
            ('DISARM', {},           'Disarming'),
        ],
        'patrol': [
            ('ARM', {},              'Arming system'),
            ('STAND', {},            'Standing up'),
            ('RUN_BEHAVIOR', {'behavior_id': 'idle_breath'}, 'Patrol ready'),
            ('FOLLOW', {},           'Following patrol route'),
            ('STAND', {},            'Patrol complete'),
            ('DISARM', {},           'Disarming'),
        ],
    }
    steps = scenarios.get(args.scenario, scenarios['companion'])
    try:
        for action, params, desc in steps:
            info(f'  {desc}...')
            body = {'action': action, **params}
            r = POST('/api/v1/command', body)
            status = '✓' if r.get('ok', True) else '✗'
            print(f'  {GREEN(status) if status == "✓" else RED(status)} {action}')
            time.sleep(float(args.delay))
        ok(f'Demo "{args.scenario}" complete')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)
    except KeyboardInterrupt:
        warn('Demo interrupted — sending E-STOP')
        try: POST('/api/v1/estop')
        except: pass


def cmd_plugin_new(args):
    """Scaffold a new CERBERUS plugin."""
    name = args.name.replace('-', '_').lower()
    trust = args.trust
    out_dir = args.output or f'./{name}'
    import os
    os.makedirs(out_dir, exist_ok=True)

    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": args.description or f'{name} CERBERUS plugin',
        "author": os.getenv('USER', 'unknown'),
        "trust_level": trust,
        "permissions": {"community": ["behaviors", "ui", "sensors"],
                        "trusted": ["behaviors", "ui", "sensors", "personality", "cognitive", "learning"]}[trust],
        "entry_point": "plugin.py",
        "cerberus_version": ">=2.0.0",
    }
    with open(f'{out_dir}/manifest.json', 'w') as f:
        json.dump(manifest, f, indent=2)

    plugin_code = textwrap.dedent(f'''
        """
        {name} — CERBERUS Plugin
        Trust level: {trust}
        Generated by: cerberus plugin new
        """
        import asyncio
        import logging
        log = logging.getLogger('cerberus.plugin.{name}')


        async def init(ctx):
            """
            Called when plugin is activated.
            ctx is a CerberusPluginContext with full API access.
            """
            # Register a custom behavior
            ctx.register_behavior({{
                'id':          '{name}_behavior',
                'name':        '{name.replace("_"," ").title()}',
                'category':    'custom',
                'icon':        '🔌',
                'duration_s':  2.0,
            }})

            # Register a UI panel
            ctx.register_ui_panel({{
                'id':    '{name}_panel',
                'title': '{name.replace("_"," ").title()}',
                'icon':  '🔌',
            }})

            # Subscribe to robot events
            ctx.on_event('fsm.transition', on_state_change)

            await ctx.emit('ready', {{'plugin': '{name}'}})
            log.info('[{name}] Initialized')


        def on_state_change(event, data):
            new_state = data.get('to', '') if isinstance(data, dict) else ''
            log.debug('[{name}] State → %s', new_state)


        def teardown(ctx):
            log.info('[{name}] Teardown')
    ''').lstrip()

    with open(f'{out_dir}/plugin.py', 'w') as f:
        f.write(plugin_code)

    readme = textwrap.dedent(f'''
        # {name}

        CERBERUS plugin — trust level: **{trust}**

        ## Setup

        1. Copy this directory to your `plugins/` folder
        2. Start the backend: `make run`
        3. Activate: `cerberus plugin activate {name}`

        ## Permissions

        {json.dumps(manifest["permissions"], indent=2)}

        ## Development

        Edit `plugin.py` and restart — CERBERUS hot-reloads plugins.
        Use `cerberus plugin list` to verify status.
    ''').lstrip()

    with open(f'{out_dir}/README.md', 'w') as f:
        f.write(readme)

    ok(f'Plugin scaffolded: {out_dir}/')
    print(f'  {DIM("manifest.json")} — plugin metadata and permissions')
    print(f'  {DIM("plugin.py")}     — plugin implementation')
    print(f'  {DIM("README.md")}     — documentation')
    info(f'Next: cp -r {out_dir} plugins/ && make run')


def cmd_plugin_list(args):
    try:
        r = GET('/api/v1/cerberus/plugins')
        section(f'CERBERUS Plugins ({r.get("active",0)}/{r.get("total",0)} active)')
        for p in r.get('plugins', []):
            s = p.get('status', 'unknown')
            sym = GREEN('●') if s == 'active' else RED('●') if s == 'error' else YELLOW('○')
            trust = p.get('trust', 'community')
            print(f'  {sym} {p["name"]:<28} [{trust}] {s}')
            if s == 'error' and p.get('error'):
                print(f'     {RED(p["error"])}')
    except RuntimeError as e:
        err(str(e)); sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# ARGUMENT PARSER
# ════════════════════════════════════════════════════════════════════════════

def build_parser():
    p = argparse.ArgumentParser(
        prog='cerberus',
        description='CERBERUS Developer CLI — Cognitive robotics platform tools',
    )
    p.add_argument('--api', default=API_URL)
    p.add_argument('--token', default=TOKEN)
    p.add_argument('--verbose', '-v', action='store_true')

    sub = p.add_subparsers(dest='command', metavar='command')

    sub.add_parser('status', help='Full CERBERUS status')

    # Mind
    mind = sub.add_parser('mind', help='Cognitive state')
    mind_sub = mind.add_subparsers(dest='mind_cmd')
    mind_sub.add_parser('memory', help='Working memory snapshot')
    goal = mind_sub.add_parser('goal', help='Goal management')
    goal_sub = goal.add_subparsers(dest='goal_cmd')
    gp = goal_sub.add_parser('push', help='Push a goal')
    gp.add_argument('name')
    gp.add_argument('--type', default='express',
                    choices=['explore','interact','patrol','express','rest','greet','custom'])
    gp.add_argument('--priority', type=float, default=0.5)
    gp.add_argument('--deadline', type=float, default=None, metavar='SECONDS')
    gd = goal_sub.add_parser('done', help='Complete active goal')
    gd.add_argument('--failed', dest='success', action='store_false')

    # Body
    body = sub.add_parser('body', help='Digital anatomy state')

    # Personality
    pers = sub.add_parser('personality', help='Mood and traits')
    pers_sub = pers.add_subparsers(dest='pers_cmd')
    me = pers_sub.add_parser('event', help='Inject mood event')
    me.add_argument('event',
                    choices=['successful_interaction','safety_trip','goal_complete',
                             'goal_failed','mission_complete','exploration_reward'])
    me.add_argument('--magnitude', type=float, default=1.0)

    # Learning
    lrn = sub.add_parser('learning', help='Learning system')
    lrn_sub = lrn.add_subparsers(dest='lrn_cmd')
    lrn_sub.add_parser('suggest', help='Get behavior suggestion')
    pf = lrn_sub.add_parser('prefer', help='Record behavior preference')
    pf.add_argument('behavior')
    pf.add_argument('--reward', type=float, default=1.0)
    rs = lrn_sub.add_parser('reset', help='Reset all learning')
    rs.add_argument('--yes', '-y', action='store_true')

    # Perception
    sub.add_parser('perception', help='Perception frame')

    # Demo
    dm = sub.add_parser('demo', help='Run behavior demo')
    dm.add_argument('scenario', nargs='?', default='companion',
                    choices=['companion', 'patrol'])
    dm.add_argument('--delay', type=float, default=2.5, metavar='SECONDS')

    # Plugin
    plug = sub.add_parser('plugin', help='Plugin management')
    plug_sub = plug.add_subparsers(dest='plug_cmd')
    plug_sub.add_parser('list', help='List plugins')
    pn = plug_sub.add_parser('new', help='Scaffold new plugin')
    pn.add_argument('name')
    pn.add_argument('--trust', default='community', choices=['community', 'trusted'])
    pn.add_argument('--description', default='')
    pn.add_argument('--output', default=None)

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()
    global API_URL, TOKEN
    if hasattr(args, 'api') and args.api:  API_URL = args.api
    if hasattr(args, 'token') and args.token: TOKEN = args.token
    if hasattr(args, 'verbose'): pass  # available to sub-commands

    if not args.command:
        parser.print_help(); return

    dispatch = {
        'status':      cmd_status,
        'body':        cmd_body,
        'perception':  cmd_perception,
        'demo':        cmd_demo,
    }

    if args.command in dispatch:
        try: dispatch[args.command](args)
        except KeyboardInterrupt: print()
        return

    if args.command == 'mind':
        mc = getattr(args, 'mind_cmd', None)
        if not mc or mc == 'status': cmd_mind(args)
        elif mc == 'memory':
            try:
                m = GET('/api/v1/cerberus/mind/memory')
                section('Working Memory')
                for item in m.get('items', []):
                    print(f'  [{item["source"]}] age={item["age_s"]:.0f}s '
                          f'imp={item["importance"]:.2f}')
            except RuntimeError as e: err(str(e))
        elif mc == 'goal':
            gc = getattr(args, 'goal_cmd', None)
            if gc == 'push': cmd_goal_push(args)
            elif gc == 'done': cmd_goal_done(args)

    elif args.command == 'personality':
        pc = getattr(args, 'pers_cmd', None)
        if not pc: cmd_personality(args)
        elif pc == 'event': cmd_mood_event(args)

    elif args.command == 'learning':
        lc = getattr(args, 'lrn_cmd', None)
        if not lc: cmd_learning(args)
        elif lc == 'suggest': cmd_suggest(args)
        elif lc == 'prefer': cmd_prefer(args)
        elif lc == 'reset': cmd_learning_reset(args)

    elif args.command == 'plugin':
        pc = getattr(args, 'plug_cmd', None)
        if not pc or pc == 'list': cmd_plugin_list(args)
        elif pc == 'new': cmd_plugin_new(args)


if __name__ == '__main__':
    main()
