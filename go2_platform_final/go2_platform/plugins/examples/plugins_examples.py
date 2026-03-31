# go2_platform/plugins/examples/funscript_plugin/manifest.json contents:
FUNSCRIPT_MANIFEST = {
    "name": "funscript_player",
    "version": "1.0.0",
    "description": "FunScript playback mapped to robot motion",
    "author": "Go2 Platform",
    "permissions": ["behaviors", "sensors", "fsm", "ui"],
    "entry_point": "plugin.py",
}

"""
go2_platform/plugins/examples/funscript_plugin/plugin.py
══════════════════════════════════════════════════════════════════════════════
FunScript Player Plugin
Demonstrates: behavior registration, FSM events, UI panel, sensor access.
"""
import asyncio
import json
import math
import time


async def init(ctx):
    """Called by PluginSystem.activate() with sandboxed context."""

    # Register custom behavior
    ctx.register_behavior({
        'id': 'funscript_play',
        'name': 'FunScript',
        'category': 'play',
        'icon': '🎬',
        'description': 'Plays a FunScript motion sequence',
        'params': ['script_url', 'speed', 'loop'],
        'duration_s': None,
    })

    # Register UI panel
    ctx.register_ui_panel({
        'id': 'funscript_panel',
        'title': 'FunScript',
        'icon': '🎬',
        'component': 'FunScriptPanel',  # Referenced by frontend
        'position': 'right',
    })

    # Hook FSM transitions
    await ctx.on_fsm_transition(_on_fsm_change)

    await ctx.emit('ready', {'version': '1.0.0'})
    print('[funscript_plugin] Initialized')


async def _on_fsm_change(event, data):
    new_state = data.get('to', '')
    if new_state == 'performing':
        print(f'[funscript_plugin] Robot entered performing state')


def teardown(ctx):
    print('[funscript_plugin] Teardown')


# ════════════════════════════════════════════════════════════════════════════

"""
go2_platform/plugins/examples/fleet_plugin/plugin.py
Fleet coordination plugin — demonstrates missions + world model access.
"""

_fleet_state = {
    'robots': {},
    'tasks': [],
}


async def init(ctx):  # noqa: F811
    ctx.register_behavior({
        'id': 'synchronized_dance',
        'name': 'Sync Dance',
        'category': 'fleet',
        'icon': '💃',
        'description': 'Synchronized multi-robot choreography',
        'duration_s': 8.0,
    })

    ctx.on_event('fsm.transition', _on_state)
    await ctx.emit('fleet_ready', {'robot_count': len(_fleet_state['robots'])})


async def _on_state(event, data):
    _fleet_state['last_state'] = data.get('to')


# ════════════════════════════════════════════════════════════════════════════

"""
go2_platform/plugins/examples/navigation_plugin/plugin.py
SLAM + Nav2 navigation plugin — demonstrates world model + missions.
"""

async def init(ctx):  # noqa: F811
    """Navigation plugin — adds waypoint nav behaviors."""

    ctx.register_behavior({
        'id': 'nav_to_waypoint',
        'name': 'Go To',
        'category': 'navigation',
        'icon': '📍',
        'description': 'Navigate to a named waypoint using Nav2/A*',
        'params': ['waypoint_id'],
        'duration_s': None,
    })

    ctx.register_behavior({
        'id': 'return_home',
        'name': 'Go Home',
        'category': 'navigation',
        'icon': '🏠',
        'description': 'Return to home zone',
        'duration_s': None,
    })

    ctx.register_ui_panel({
        'id': 'nav_map_panel',
        'title': 'Navigation Map',
        'icon': '🗺️',
        'component': 'NavMapPanel',
        'position': 'center',
    })

    ctx.register_route('POST', '/navigate', _handle_navigate)
    ctx.register_route('GET', '/waypoints', _get_waypoints)

    await ctx.emit('nav_ready', {})


async def _handle_navigate(request):
    wp_id = request.get('waypoint_id')
    return {'ok': True, 'navigating_to': wp_id}


async def _get_waypoints():
    return {'waypoints': []}
