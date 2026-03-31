# UndercarriagePayload Plugin

**Version:** 1.0.0 | **Trust:** TRUSTED | **Min CERBERUS:** 2.2.0

Manages compliant substructures (silicone pads, sensor arrays, protective
underbellies) mounted to the underside of the Go2 body frame.

---

## What it does

Attaching mass below the robot changes **five** physical parameters
simultaneously. This plugin handles all of them automatically:

| Parameter | Effect | Auto-compensation |
|---|---|---|
| Reduced ground clearance | Payload can drag on terrain | Raises standing height, tightens min_body_height |
| Increased total mass | Higher joint loads, slower safe speed | Reduces max_vx / max_vy |
| Increased rotational inertia | Slower safe yaw rate | Reduces max_vyaw |
| Lower combined COM | Tighter safe tilt angle before belly drag | Reduces max_roll_deg / max_pitch_deg |
| Swing-phase clearance | Foot may snag on rough terrain | Increases foot_raise_height |

---

## Supported Materials

| Material | Friction | Compliance | Use case |
|---|---|---|---|
| `silicone` | High (0.9) | 8 mm | Tactile sensing, protection, gentle nudging |
| `rigid_plate` | Medium (0.5) | 0 mm | Sensor arrays, hard-mount equipment |
| `foam` | High (0.8) | 15 mm | Impact absorption, inspection padding |
| `mesh` | Low (0.4) | 2 mm | Ventilated lightweight underbelly |

---

## Autonomous Behaviors

### 1. `ground_scout`
Lower belly to ~3 mm above terrain and slowly traverse while the compliant
surface reads terrain contact texture via foot-force redistribution.

**Trigger:** curiosity > 0.5 AND speed < 0.1 m/s AND not scouted within 2 min  
**REST:** `POST /payload/behavior/ground_scout`  
**Events:** `payload.scout_sample` (continuous), `payload.behavior`

### 2. `belly_contact`
Controlled touchdown — lowers robot until silicone makes full ground contact,
holds for N seconds, then rises. Aborts immediately on drag detection.

**Trigger:** Direct API only (safety-sensitive)  
**REST:** `POST /payload/behavior/belly_contact`  
**Events:** `payload.contact_hold` (during hold), `payload.behavior`

### 3. `thermal_rest`
Execute `stand_down` so the robot rests on the silicone pad. LED shifts to
amber during hold. Good for low-energy states, heated/cold surfaces.

**Trigger:** boredom > 0.6 AND battery > 20% AND stationary  
**REST:** `POST /payload/behavior/thermal_rest`  
**Events:** `payload.thermal_rest` (~1Hz status)

### 4. `object_nudge`
Lower belly to contact height, advance to push a detected ground-level object
using the high-friction silicone surface, then retreat and rise.

**Trigger:** obstacle_near AND playfulness > 0.7  
**REST:** `POST /payload/behavior/object_nudge`  
**Events:** `payload.behavior`

### 5. `substrate_scan`
Systematic boustrophedon (back-and-forth) belly traverse building a tactile
map. Each tile records contact state and foot force distribution.

**Trigger:** Direct API only  
**REST:** `POST /payload/behavior/substrate_scan`  
**Events:** `payload.scan_result` (on completion with full tile map)

---

## Safety Features

- **Drag detection:** lateral motion while in contact → immediate `stop_move()`
  and `payload.drag_warning` event
- **Height lock:** `min_body_height` is raised in SafetyWatchdog so the robot
  cannot be commanded below safe clearance while payload is attached
- **Tilt limits reduced:** max_roll / max_pitch tightened to prevent belly drag
  at incline angles
- **Speed limits reduced:** proportional to payload mass fraction
- **E-stop passthrough:** all behaviors abort and restore height on E-stop
- **Restore on detach:** calling `detach()` restores all original limits,
  default gait, and nominal body height regardless of current behavior state

---

## REST API

```
GET  /payload                         — status, contact state, compensator values
POST /payload/attach                  — attach payload (see schema below)
POST /payload/detach                  — remove payload, restore defaults
POST /payload/behavior/ground_scout   — {duration_s: float}
POST /payload/behavior/belly_contact  — {hold_s: float}
POST /payload/behavior/thermal_rest   — {duration_s: float}
POST /payload/behavior/object_nudge   — {nudge_speed: float, nudge_dist_m: float}
POST /payload/behavior/substrate_scan — {cols: int, col_width_m: float, row_len_m: float}
```

### Attach schema

```json
{
  "name": "silicone_pad_v1",
  "material": "silicone",
  "mass_kg": 1.5,
  "thickness_m": 0.050,
  "length_m": 0.300,
  "width_m": 0.200,
  "desired_clearance_m": 0.025,
  "has_tactile_sensor": true,
  "has_thermal_sensor": false
}
```

---

## WebSocket Events

All payload events are forwarded to connected WS clients as:
```json
{"type": "payload", "data": { ... }}
```

Key topics: `payload.contact`, `payload.drag_warning`, `payload.behavior`,
`payload.scan_result`, `payload.attached`, `payload.detached`.

---

## Physics Constants (Go2-specific)

```
BELLY_OFFSET              = 0.120 m   (body COM → belly surface)
NOMINAL_BODY_HEIGHT       = 0.270 m
NOMINAL_BELLY_CLEARANCE   = 0.150 m   (at default standing height)
OPERATIONAL_CLEARANCE     = 0.025 m   (safety buffer above contact)
SILICONE_COMPRESSION      = 0.008 m   (at full robot weight)
```

Contact height formula:
```
contact_h = BELLY_OFFSET + thickness - compliance
           = 0.120 + thickness - 0.008
```

For default 50mm silicone: `contact_h = 0.162 m`  
Recommended standing height: `0.162 + 0.025 = 0.187 m`
(hardware minimum 0.20 m takes precedence, providing extra margin)

---

## Installation

The plugin is auto-discovered if `plugins/` is in `PLUGIN_DIRS`:

```bash
# .env
PLUGIN_DIRS=plugins
```

No additional dependencies required.
