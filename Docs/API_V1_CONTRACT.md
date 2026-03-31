# API v1 Contract Bootstrap Specification

## 1. Scope

This document defines the canonical versioned backend contract for `CERBERUS-UnitreeGo2CompanionAPI`.

The objective is to stabilize the controller-facing and plugin-facing surface before deeper refactors are performed. The document is intentionally implementation-oriented. It exists to support the Sweet-Freedom convergence plan and to reduce contract drift between:

- `CERBERUS-UnitreeGo2CompanionAPI`
- `CERBERUS-UnitreeGo2Companion_Web-Interface`
- `Sweetie-Bot-Plugins_for_CERBERUS-API`
- future ROS 2 bridge integration work

## 2. Design Objectives

### 2.1 Primary objectives

1. Introduce a canonical versioned namespace rooted at `/api/v1`.
2. Preserve all existing unversioned routes as compatibility aliases during transition.
3. Stabilize WebSocket message semantics while preserving the current `{type, data}` payload shape.
4. Provide a contract baseline that the web interface and external Sweetie plugins can target.
5. Ensure that authentication, safety gating, and command validation rules remain authoritative at the backend.

### 2.2 Non-objectives for this phase

1. This phase does not replace the internal engine architecture.
2. This phase does not finalize persona, speech, or Nav2 payload schemas.
3. This phase does not remove legacy routes.
4. This phase does not replace in-process plugin loading.

## 3. Versioning Policy

### 3.1 Canonical namespace

All newly stabilized routes SHALL be exposed under `/api/v1`.

### 3.2 Compatibility policy

Existing routes SHALL remain active and SHALL return semantically equivalent payloads.

Examples:

- `/state` remains valid.
- `/api/v1/robot/state` becomes canonical.
- `/plugins` remains valid.
- `/api/v1/plugins` becomes canonical.

### 3.3 Deprecation policy

Legacy unversioned routes SHALL only be considered for removal after:

1. the web interface consumes `/api/v1` exclusively,
2. plugin integrations no longer depend on legacy routes,
3. a migration note has been published.

## 4. Authentication Rules

### 4.1 REST

All authenticated REST routes SHALL accept:

- `X-CERBERUS-Key` request header.

### 4.2 WebSocket

The WebSocket endpoint SHALL accept:

- `?api_key=` query parameter.

### 4.3 Auth-exempt endpoints

The following endpoints SHALL remain unauthenticated for orchestrators and health probes:

- `/health`
- `/ready`
- `/api/v1/system/health`
- `/api/v1/system/ready`

## 5. Canonical REST Endpoint Map

## 5.1 System

| Canonical route | Legacy alias | Notes |
|---|---|---|
| `GET /api/v1/system/info` | `GET /` | backend status, version, engine state, simulation flag |
| `GET /api/v1/system/health` | `GET /health` | liveness |
| `GET /api/v1/system/ready` | `GET /ready` | readiness |
| `GET /api/v1/session` | `GET /session` | session state and persisted personality |

## 5.2 Robot state

| Canonical route | Legacy alias |
|---|---|
| `GET /api/v1/robot/state` | `GET /state` |
| `GET /api/v1/robot/stats` | `GET /stats` |
| `GET /api/v1/robot/anatomy` | `GET /anatomy` |
| `GET /api/v1/robot/behavior` | `GET /behavior` |
| `GET /api/v1/robot/terrain` | `GET /terrain` |
| `GET /api/v1/robot/stair` | `GET /stair` |
| `GET /api/v1/robot/limb_loss` | `GET /limb_loss` |
| `GET /api/v1/robot/voice` | `GET /voice` |
| `GET /api/v1/robot/payload` | `GET /payload` |

## 5.3 Safety

| Canonical route | Legacy alias |
|---|---|
| `GET /api/v1/safety/events` | `GET /safety/events` |
| `POST /api/v1/safety/estop` | `POST /safety/estop` |
| `POST /api/v1/safety/clear_estop` | `POST /safety/clear_estop` |

## 5.4 Motion

| Canonical route | Legacy alias |
|---|---|
| `POST /api/v1/robot/motion/stand_up` | `POST /motion/stand_up` |
| `POST /api/v1/robot/motion/stand_down` | `POST /motion/stand_down` |
| `POST /api/v1/robot/motion/stop` | `POST /motion/stop` |
| `POST /api/v1/robot/motion/move` | `POST /motion/move` |
| `POST /api/v1/robot/motion/body_height` | `POST /motion/body_height` |
| `POST /api/v1/robot/motion/euler` | `POST /motion/euler` |
| `POST /api/v1/robot/motion/gait` | `POST /motion/gait` |
| `POST /api/v1/robot/motion/foot_raise` | `POST /motion/foot_raise` |
| `POST /api/v1/robot/motion/speed_level` | `POST /motion/speed_level` |
| `POST /api/v1/robot/motion/continuous_gait` | `POST /motion/continuous_gait` |
| `POST /api/v1/robot/motion/sport_mode` | `POST /motion/sport_mode` |

## 5.5 Peripherals

| Canonical route | Legacy alias |
|---|---|
| `POST /api/v1/robot/led` | `POST /led` |
| `POST /api/v1/robot/audio/volume` | `POST /volume` |
| `POST /api/v1/robot/navigation/obstacle_avoidance` | `POST /obstacle_avoidance` |

## 5.6 Behavior and cognition

| Canonical route | Legacy alias |
|---|---|
| `POST /api/v1/behavior/goal` | `POST /behavior/goal` |

## 5.7 Plugins

| Canonical route | Legacy alias | Notes |
|---|---|---|
| `GET /api/v1/plugins` | `GET /plugins` | merged plugin listing target |
| `POST /api/v1/plugins/{name}/enable` | `POST /plugins/{name}/enable` | |
| `POST /api/v1/plugins/{name}/disable` | `POST /plugins/{name}/disable` | |
| `DELETE /api/v1/plugins/{name}` | `DELETE /plugins/{name}` | |
| `POST /api/v1/plugins/{name}/execute` | none yet | reserved for Sweetie-style HTTP plugins |

## 6. Response Shape Rules

### 6.1 Success responses

Existing success helper format MAY remain for command routes during transition:

```json
{
  "ok": true
}
```

Command routes MAY include additional fields:

```json
{
  "ok": true,
  "mode": "dance1"
}
```

### 6.2 Error responses

A normalized error envelope SHALL be introduced incrementally.

Target shape:

```json
{
  "ok": false,
  "error": {
    "code": "string",
    "message": "string",
    "details": {}
  }
}
```

During transition, existing FastAPI `HTTPException` output MAY still appear on legacy routes.

## 7. WebSocket Contract

## 7.1 Compatibility requirement

Current clients depend on:

```json
{
  "type": "state",
  "data": {}
}
```

This shape SHALL remain valid.

## 7.2 Canonical envelope

The canonical envelope SHALL extend the existing format without breaking it:

```json
{
  "type": "state|event|command_ack|plugin_status|error|ping",
  "ts": "RFC3339 timestamp",
  "seq": 123,
  "data": {}
}
```

Rules:

1. `type` is mandatory.
2. `data` is mandatory except for keepalive messages.
3. `ts` and `seq` are optional for transition compatibility.
4. clients SHALL ignore unknown top-level fields.

## 7.3 Message classes

### 7.3.1 State classes already present

The following message types are already aligned to the current backend event bus flow and SHALL be preserved:

- `state`
- `terrain`
- `stair`
- `payload`
- `voice`
- `limb_loss`

### 7.3.2 New message classes

The following message types SHALL be added during contract stabilization:

- `command_ack`
- `plugin_status`
- `error`
- `ping`

## 7.4 WebSocket command input contract

Current inbound command shape:

```json
{
  "cmd": "move",
  "vx": 0.2,
  "vy": 0.0,
  "vyaw": 0.0
}
```

This SHALL remain valid.

Minimum command set for compatibility:

- `move`
- `stop`
- `estop`
- `sport_mode`
- `body_height`
- `led`
- `subscribe`

## 8. Safety and Validation Rules

1. WebSocket commands SHALL traverse the same safety checks as REST commands whenever practical.
2. E-stop state SHALL remain authoritative over all motion-producing routes.
3. Heartbeat updates SHALL continue to be issued for active motion control surfaces.
4. Plugin-originated robot commands SHALL be backend-gated and SHALL NOT bypass safety checks.

## 9. Sweetie Plugin Integration Reservation

The canonical API reserves the following route for the future HTTP plugin runtime:

- `POST /api/v1/plugins/{name}/execute`

The request and response payloads for that route SHALL align with the Sweetie schemas:

- `schemas/execute.request.schema.json`
- `schemas/execute.response.schema.json`

This document does not redefine those payloads.

## 10. Immediate Implementation Checklist

### 10.1 Phase 1

- [ ] add `/api/v1` route aliases for existing system, robot, safety, and plugin list endpoints
- [ ] exempt `/api/v1/system/health` and `/api/v1/system/ready` from auth
- [ ] permit `X-CERBERUS-Key` in CORS allow-headers
- [ ] stop hardcoding backend version values where dynamic version import already exists
- [ ] add `ts` metadata to WebSocket messages while preserving `{type, data}`
- [ ] add `command_ack` WebSocket messages for accepted and failed commands

### 10.2 Phase 2

- [ ] add `plugin_status` WebSocket stream
- [ ] add `/api/v1/plugins/{name}/execute`
- [ ] adopt normalized backend error envelope
- [ ] publish OpenAPI examples for canonical routes

## 11. Acceptance Criteria

### 11.1 REST

The bootstrap is accepted when:

1. `/api/v1/system/health` returns the same liveness semantics as `/health`.
2. `/api/v1/system/ready` returns the same readiness semantics as `/ready`.
3. `/api/v1/robot/state` returns the same payload class as `/state`.
4. `/api/v1/plugins` returns the same payload class as `/plugins`.
5. legacy routes continue to function.

### 11.2 WebSocket

The bootstrap is accepted when:

1. existing clients that only read `{type, data}` continue to function,
2. new clients can consume `ts` and `seq` when present,
3. command failures are surfaced as structured WebSocket errors,
4. command success and acceptance can be surfaced through `command_ack`.

## 12. Change Control

Any future change to canonical endpoint names, WebSocket message classes, or compatibility policy SHALL update this document in the same pull request.