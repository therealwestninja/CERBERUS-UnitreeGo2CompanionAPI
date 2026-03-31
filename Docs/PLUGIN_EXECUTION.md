# Plugin Execution Contract

Defines HTTP execution surface for external (Sweetie-style) plugins.

## Endpoint

POST /api/v1/plugins/{name}/execute

## Request

{
  "action": "string",
  "params": {},
  "request_id": "optional"
}

## Response

{
  "ok": true,
  "result": {}
}

or

{
  "ok": false,
  "error": {
    "code": "string",
    "message": "string"
  }
}

## Notes

- Execution is sandboxed via backend plugin manager
- Safety checks MUST run before any robot-affecting action
- This endpoint is transport-compatible with Sweetie plugin model
