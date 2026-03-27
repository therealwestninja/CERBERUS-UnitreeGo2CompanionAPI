"""
go2_platform/backend/cli/go2_cli.py
══════════════════════════════════════════════════════════════════════════════
Go2 Platform CLI — Developer Toolkit

Commands:
  go2 status              Show platform status
  go2 arm / disarm        Arm/disarm the robot
  go2 estop               Trigger emergency stop
  go2 command <action>    Send any platform command
  go2 behavior <id>       Run a named behavior
  go2 behaviors           List all behaviors
  go2 objects list        List object registry
  go2 objects add         Add object interactively
  go2 objects export      Export object registry
  go2 objects import      Import object registry
  go2 mission create      Create a mission
  go2 mission start <id>  Start a mission
  go2 i18n list           List available locales
  go2 i18n set <code>     Switch locale
  go2 animation load      Load an animation file
  go2 animation play      Play loaded animation
  go2 health              Show health check results
  go2 metrics             Show key metrics
  go2 logs                Stream platform logs (WebSocket)
  go2 server              Start the platform backend
  go2 test                Run the test suite
  go2 config show         Display current configuration
  go2 config set          Set a configuration value

Usage:
  python -m backend.cli.go2_cli [command] [args...]
  go2 [command]  (when installed as package entry point)

Environment:
  GO2_API_URL   Platform API URL (default: http://localhost:8080)
  GO2_TOKEN     API Bearer token
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional
from urllib import request, error


# ── Config ────────────────────────────────────────────────────────────────

API_URL = os.getenv('GO2_API_URL', 'http://localhost:8080')
TOKEN   = os.getenv('GO2_TOKEN', os.getenv('GO2_API_TOKEN', ''))

# ANSI color codes (disabled on Windows without ANSI support)
_COLOR = sys.platform != 'win32' or os.getenv('FORCE_COLOR')
def _c(code: str, text: str) -> str:
    return f'\033[{code}m{text}\033[0m' if _COLOR else text

GREEN  = lambda t: _c('32', t)
RED    = lambda t: _c('31', t)
YELLOW = lambda t: _c('33', t)
CYAN   = lambda t: _c('36', t)
BOLD   = lambda t: _c('1',  t)
DIM    = lambda t: _c('2',  t)


# ── HTTP helpers ──────────────────────────────────────────────────────────

class APIError(Exception):
    pass


def _req(method: str, path: str, body: Optional[dict] = None,
         timeout: float = 10.0) -> dict:
    url = f'{API_URL}{path}'
    data = json.dumps(body).encode() if body else None
    headers = {'Content-Type': 'application/json'}
    if TOKEN:
        headers['Authorization'] = f'Bearer {TOKEN}'
    try:
        req = request.Request(url, data=data, headers=headers, method=method)
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get('detail', str(e))
        except Exception:
            detail = str(e)
        raise APIError(f'HTTP {e.code}: {detail}')
    except error.URLError as e:
        raise APIError(f'Connection failed to {API_URL}: {e.reason}')


def GET(path: str)                -> dict: return _req('GET', path)
def POST(path: str, body: dict={}) -> dict: return _req('POST', path, body)
def DELETE(path: str)              -> dict: return _req('DELETE', path)
def PATCH(path: str, body: dict)   -> dict: return _req('PATCH', path, body)


# ── Output helpers ────────────────────────────────────────────────────────

def ok(msg: str): print(GREEN('✓') + ' ' + msg)
def err(msg: str): print(RED('✗') + ' ' + msg, file=sys.stderr)
def warn(msg: str): print(YELLOW('⚠') + ' ' + msg)
def info(msg: str): print(CYAN('ℹ') + ' ' + msg)
def section(title: str): print('\n' + BOLD(title))
def row(label: str, value: str, ok_val: bool = None):
    if ok_val is True:
        val_str = GREEN(str(value))
    elif ok_val is False:
        val_str = RED(str(value))
    else:
        val_str = str(value)
    print(f'  {DIM(label + ":"): <24} {val_str}')


# ════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════════════════

def cmd_status(args):
    """Show full platform status."""
    try:
        s = GET('/api/v1/status')
        section('Platform Status')
        platform = s.get('platform', {})
        row('Version',     platform.get('version', '?'))
        row('Mode',        platform.get('mode', '?') if 'mode' in platform
                           else ('SIM' if platform.get('sim') else 'HW'))

        section('Robot State')
        fsm = s.get('fsm', {})
        state  = fsm.get('state', '?')
        armed  = fsm.get('armed', False)
        row('State',  state, ok_val=(state not in ('estop','fault','offline')))
        row('Armed',  'YES' if armed else 'NO', ok_val=armed)
        row('Elapsed', f'{fsm.get("elapsed_s", 0):.1f}s')

        section('Safety')
        safety = s.get('safety', {})
        level = safety.get('level', '?')
        row('Level',   level, ok_val=(level == 'normal'))
        row('Trips',   str(safety.get('trips', 0)),  ok_val=(safety.get('trips', 0) == 0))
        row('E-Stops', str(safety.get('estops', 0)), ok_val=(safety.get('estops', 0) == 0))

        section('Telemetry')
        tel = s.get('telemetry', {})
        bat = tel.get('battery_pct', 0)
        row('Battery', f'{bat:.0f}%', ok_val=(bat > 30))
        row('Voltage', f'{tel.get("voltage", 0):.1f}V')
        row('Pitch',   f'{tel.get("pitch_deg", 0):.1f}°',
            ok_val=(abs(tel.get("pitch_deg", 0)) < 5))

        section('Objects / Missions')
        row('Objects',        str(s.get('objects', 0)))
        row('Zones',          str(s.get('zones', 0)))
        row('Active mission', str(s.get('mission', 'none')))

    except APIError as e:
        err(str(e))
        sys.exit(1)


def cmd_arm(args):
    try:
        r = POST('/api/v1/arm')
        ok(r.get('msg', 'Armed'))
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_disarm(args):
    try:
        r = POST('/api/v1/disarm')
        ok(r.get('msg', 'Disarmed'))
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_estop(args):
    try:
        POST('/api/v1/estop')
        warn('E-STOP triggered — all motion halted')
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_command(args):
    action = args.action.upper()
    params = {}
    if args.params:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError:
            err('--params must be valid JSON'); sys.exit(1)
    try:
        r = POST('/api/v1/command', {'action': action, **params})
        ok(f'{action}: {json.dumps(r)}')
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_behaviors(args):
    try:
        r = GET('/api/v1/behaviors')
        section('Available Behaviors')
        cats = r.get('categories', {})
        for cat, behs in cats.items():
            print(f'\n  {BOLD(cat.title())}')
            for b in behs:
                dur = f'{b["duration_s"]:.1f}s' if b.get('duration_s') else 'cont.'
                print(f'    {b.get("icon","🐾")} {b["id"]:<20} {DIM(b["name"])} ({dur})')
        print(f'\n  Active policy: {CYAN(r.get("active_policy","?"))}')
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_behavior(args):
    try:
        r = POST(f'/api/v1/behaviors/{args.id}/run')
        ok(f'Running behavior: {args.id}')
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_objects(args):
    if args.subcommand == 'list':
        try:
            r = GET('/api/v1/objects')
            section(f'Object Registry ({r["count"]} objects)')
            for obj in r.get('objects', []):
                affs = ', '.join(obj.get('affordances', [])[:3])
                print(f'  {CYAN(obj["id"]):<32} {obj["type"]:<16} {DIM(affs)}')
        except APIError as e:
            err(str(e)); sys.exit(1)

    elif args.subcommand == 'export':
        try:
            r = GET('/api/v1/world/export')
            out = args.file or f'go2_world_{int(time.time())}.json'
            with open(out, 'w') as f:
                json.dump(r, f, indent=2)
            ok(f'Exported to {out}')
        except APIError as e:
            err(str(e)); sys.exit(1)

    elif args.subcommand == 'import':
        try:
            with open(args.file) as f:
                data = json.load(f)
            r = POST('/api/v1/world/import', data)
            ok(f'Imported: {r.get("added",0)} objects, {r.get("validation_errors",[]) or 0} errors')
        except (FileNotFoundError, json.JSONDecodeError) as e:
            err(str(e)); sys.exit(1)
        except APIError as e:
            err(str(e)); sys.exit(1)


def cmd_i18n(args):
    if args.subcommand == 'list':
        try:
            r = GET('/api/v1/i18n/locales')
            section(f'Available Locales (current: {r["current"]})')
            for loc in r.get('locales', []):
                current = ' ← current' if loc['code'] == r['current'] else ''
                rtl = ' [RTL]' if loc.get('rtl') == 'true' else ''
                print(f'  {loc["flag"]} {BOLD(loc["code"]):<6} {loc["name"]}{DIM(current+rtl)}')
        except APIError as e:
            err(str(e)); sys.exit(1)

    elif args.subcommand == 'set':
        try:
            r = POST(f'/api/v1/i18n/locale/{args.code}')
            ok(f'Locale set: {r["code"]} ({r["name"]})')
        except APIError as e:
            err(str(e)); sys.exit(1)

    elif args.subcommand == 'coverage':
        try:
            r = GET('/api/v1/i18n/coverage')
            section('Translation Coverage')
            for code, stats in sorted(r.items()):
                pct = stats['coverage_pct']
                bar = '█' * int(pct // 5) + '░' * (20 - int(pct // 5))
                color = GREEN if pct >= 80 else YELLOW if pct >= 50 else RED
                print(f'  {code:<4} {color(bar)} {pct:.0f}%')
        except APIError as e:
            err(str(e)); sys.exit(1)


def cmd_health(args):
    try:
        r = GET('/api/v1/health')
        overall = r.get('status', '?')
        color = GREEN if overall == 'ok' else YELLOW if overall == 'degraded' else RED
        section(f'Health: {color(overall.upper())}')
        for check in r.get('checks', []):
            s = check['status']
            sym = GREEN('✓') if s == 'ok' else YELLOW('⚠') if s == 'degraded' else RED('✗')
            lat = f'{check.get("latency_ms", 0):.1f}ms'
            msg = check.get('message', '')
            print(f'  {sym} {check["name"]:<20} {DIM(lat)}'
                  + (f' — {msg}' if msg else ''))
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_metrics(args):
    try:
        r = GET('/api/v1/metrics/json')
        section('Platform Metrics')
        row('Uptime',      f'{r.get("uptime_s", 0):.1f}s')
        row('Commands',    str(int(r.get('commands', 0))))
        row('E-Stops',     str(int(r.get('estops', 0))),
            ok_val=(r.get('estops', 0) == 0))
        row('Safety trips', str(int(r.get('safety_trips', 0))),
            ok_val=(r.get('safety_trips', 0) == 0))
        row('Battery',     f'{r.get("battery_pct", 0):.0f}%',
            ok_val=(r.get('battery_pct', 0) > 30))
        row('WS clients',  str(int(r.get('ws_clients', 0))))
        row('Cmd latency (p95)', f'{r.get("cmd_p95_ms", 0):.1f}ms',
            ok_val=(r.get('cmd_p95_ms', 0) < 50))
        if args.prometheus:
            print('\n' + DIM('--- Prometheus format ---'))
            print(GET('/api/v1/metrics'))
    except APIError as e:
        err(str(e)); sys.exit(1)


def cmd_server(args):
    """Start the platform backend server."""
    env = os.environ.copy()
    if args.sim:
        env['GO2_MODE'] = 'simulation'
    elif args.hw:
        env['GO2_MODE'] = 'hardware'
    if args.debug:
        env['GO2_LOG_LEVEL'] = 'debug'

    info(f'Starting Go2 Platform server on port {args.port}...')
    try:
        subprocess.run([
            sys.executable, '-m', 'uvicorn',
            'backend.api.server:create_app',
            '--factory',
            '--host', args.host,
            '--port', str(args.port),
            '--reload' if args.reload else '--no-access-log',
        ], env=env)
    except KeyboardInterrupt:
        info('Server stopped')


def cmd_test(args):
    """Run the test suite."""
    info('Running Go2 Platform test suite...')
    result = subprocess.run(
        [sys.executable, 'tests/test_platform.py'],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )
    sys.exit(result.returncode)


def cmd_config(args):
    if args.subcommand == 'show':
        try:
            r = GET('/api/v1/status')
            safety = GET('/api/v1/safety')
            section('Configuration')
            row('Mode',    r.get('platform', {}).get('mode', '?'))
            row('Version', r.get('platform', {}).get('version', '?'))
            section('Safety Thresholds')
            cfg = safety.get('cfg', {})
            row('Pitch limit',   f'{cfg.get("pitch_limit_deg", "?")}°')
            row('Roll limit',    f'{cfg.get("roll_limit_deg", "?")}°')
            row('Force limit',   f'{cfg.get("force_limit_n", "?")}N')
            row('Temp limit',    f'{cfg.get("temp_limit_c", "?")}°C')
            row('Battery min',   f'{cfg.get("battery_min_pct", "?")}%')
            row('Watchdog',      f'{cfg.get("watchdog_s", "?")}s')
        except APIError as e:
            err(str(e)); sys.exit(1)

    elif args.subcommand == 'set':
        field_map = {
            'pitch': 'pitch_limit_deg',
            'roll': 'roll_limit_deg',
            'force': 'force_limit_n',
            'temp': 'temp_limit_c',
            'battery': 'battery_min_pct',
            'watchdog': 'watchdog_s',
        }
        if args.key not in field_map:
            err(f'Unknown config key. Valid: {", ".join(field_map)}')
            sys.exit(1)
        try:
            val = float(args.value)
        except ValueError:
            err('Value must be numeric'); sys.exit(1)
        try:
            r = PATCH('/api/v1/safety/config', {field_map[args.key]: val})
            ok(f'Set {args.key} = {val}: {r.get("ok")}')
        except APIError as e:
            err(str(e)); sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# ARGUMENT PARSER
# ════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='go2',
        description='Go2 Platform CLI — developer toolkit for the Unitree Go2 robotics platform',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--api', default=API_URL, metavar='URL',
                   help=f'Platform API URL (default: {API_URL})')
    p.add_argument('--token', default=TOKEN, metavar='TOKEN',
                   help='Bearer token for authentication')
    p.add_argument('--json', action='store_true', help='Output raw JSON')

    sub = p.add_subparsers(dest='command', metavar='command')

    # status
    sub.add_parser('status', help='Show platform status')

    # arm / disarm / estop
    sub.add_parser('arm',    help='Arm the robot')
    sub.add_parser('disarm', help='Disarm the robot')
    sub.add_parser('estop',  help='Trigger emergency stop')

    # command
    cmd = sub.add_parser('command', help='Send a platform command')
    cmd.add_argument('action', help='Action name (e.g., SIT, STAND, WALK)')
    cmd.add_argument('--params', metavar='JSON', help='Additional params as JSON string')

    # behaviors
    sub.add_parser('behaviors', help='List all behaviors')
    beh = sub.add_parser('behavior', help='Run a behavior')
    beh.add_argument('id', help='Behavior ID (e.g., zoomies, tail_wag)')

    # objects
    obj = sub.add_parser('objects', help='Manage object registry')
    obj_sub = obj.add_subparsers(dest='subcommand')
    obj_sub.add_parser('list', help='List all objects')
    obj_exp = obj_sub.add_parser('export', help='Export object registry')
    obj_exp.add_argument('--file', '-f', help='Output file path')
    obj_imp = obj_sub.add_parser('import', help='Import object registry')
    obj_imp.add_argument('file', help='JSON file to import')

    # i18n
    i18n = sub.add_parser('i18n', help='Localization settings')
    i18n_sub = i18n.add_subparsers(dest='subcommand')
    i18n_sub.add_parser('list', help='List available locales')
    i18n_cov = i18n_sub.add_parser('coverage', help='Translation coverage report')
    i18n_set = i18n_sub.add_parser('set', help='Switch active locale')
    i18n_set.add_argument('code', help='Locale code (e.g., en, es, ja)')

    # health
    sub.add_parser('health', help='Show health check results')

    # metrics
    met = sub.add_parser('metrics', help='Show platform metrics')
    met.add_argument('--prometheus', action='store_true', help='Also show Prometheus format')

    # server
    srv = sub.add_parser('server', help='Start the platform backend')
    srv.add_argument('--host', default='0.0.0.0')
    srv.add_argument('--port', type=int, default=8080)
    srv.add_argument('--sim', action='store_true', help='Simulation mode')
    srv.add_argument('--hw',  action='store_true', help='Hardware mode')
    srv.add_argument('--debug', action='store_true')
    srv.add_argument('--reload', action='store_true', help='Auto-reload on code change')

    # test
    sub.add_parser('test', help='Run the test suite')

    # config
    cfg = sub.add_parser('config', help='Configuration management')
    cfg_sub = cfg.add_subparsers(dest='subcommand')
    cfg_sub.add_parser('show', help='Show current configuration')
    cfg_set = cfg_sub.add_parser('set', help='Set a configuration value')
    cfg_set.add_argument('key', choices=['pitch','roll','force','temp','battery','watchdog'])
    cfg_set.add_argument('value', help='Numeric value')

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Override API URL and token from args
    global API_URL, TOKEN
    if args.api:   API_URL = args.api
    if args.token: TOKEN   = args.token

    dispatch = {
        'status':    cmd_status,
        'arm':       cmd_arm,
        'disarm':    cmd_disarm,
        'estop':     cmd_estop,
        'command':   cmd_command,
        'behaviors': cmd_behaviors,
        'behavior':  cmd_behavior,
        'objects':   cmd_objects,
        'i18n':      cmd_i18n,
        'health':    cmd_health,
        'metrics':   cmd_metrics,
        'server':    cmd_server,
        'test':      cmd_test,
        'config':    cmd_config,
    }

    if not args.command:
        parser.print_help()
        sys.exit(0)

    fn = dispatch.get(args.command)
    if fn:
        try:
            fn(args)
        except KeyboardInterrupt:
            print()
            sys.exit(0)
    else:
        err(f'Unknown command: {args.command}')
        sys.exit(1)


if __name__ == '__main__':
    main()
