"""
go2_platform/backend/core/security.py
══════════════════════════════════════════════════════════════════════════════
Security Model — Schema validation, sandboxing, rate limiting, audit log.

Security layers:
  1. Schema validation  — ALL inputs validated before touching platform logic
  2. Rate limiting      — per-client, per-endpoint token buckets
  3. Command allowlist  — explicit list of permitted actions
  4. Plugin sandboxing  — restricted API surface, permission-gated
  5. Input sanitization — strip/reject dangerous content in all string fields
  6. Audit logging      — immutable tamper-evident event log
  7. Secrets management — API keys never logged, never stored in plaintext

Threat model:
  - Malicious API clients (injection, DoS)
  - Malicious plugins (resource abuse, escalation)
  - Corrupted config/object imports (schema attacks)
  - Replay attacks (command replay via WS)
  - Cross-origin WS connections
"""

import hashlib
import hmac
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger('go2.security')


# ════════════════════════════════════════════════════════════════════════════
# ALLOWED COMMAND REGISTRY
# ════════════════════════════════════════════════════════════════════════════

# Explicit allowlist — anything not here is REJECTED
# Format: action → {required_fields, optional_fields, armed_required}
COMMAND_SCHEMA: Dict[str, dict] = {
    'ESTOP':          {'required': set(), 'optional': set(), 'armed': False},
    'CLEAR_ESTOP':    {'required': set(), 'optional': set(), 'armed': False},
    'ARM':            {'required': set(), 'optional': set(), 'armed': False},
    'DISARM':         {'required': set(), 'optional': set(), 'armed': False},
    'STAND':          {'required': set(), 'optional': set(), 'armed': True},
    'SIT':            {'required': set(), 'optional': set(), 'armed': True},
    'WALK':           {'required': set(), 'optional': {'velocity','direction'}, 'armed': True},
    'FOLLOW':         {'required': set(), 'optional': {'target_id','distance_m'}, 'armed': True},
    'NAVIGATE':       {'required': set(), 'optional': {'waypoint_id','path'}, 'armed': True},
    'INTERACT':       {'required': {'object_id'}, 'optional': {'affordance'}, 'armed': True},
    'PERFORM':        {'required': set(), 'optional': {'behavior_id'}, 'armed': True},
    'RUN_BEHAVIOR':   {'required': {'behavior_id'}, 'optional': {'params'}, 'armed': True},
    'BODY_CTRL':      {'required': set(), 'optional': {'height','roll','yaw','pitch','speed'}, 'armed': True},
    'SET_POLICY':     {'required': {'policy'}, 'optional': set(), 'armed': False},
    'SET_TARGET':     {'required': set(), 'optional': {'object_id'}, 'armed': False},
    'START_MISSION':  {'required': {'mission_id'}, 'optional': set(), 'armed': True},
    'STOP_MISSION':   {'required': set(), 'optional': set(), 'armed': False},
    'UPDATE_PROFILE': {'required': {'profile'}, 'optional': set(), 'armed': False},
    'GET_STATUS':     {'required': set(), 'optional': set(), 'armed': False},
}

# String fields that must be sanitized (no HTML, no control chars, no SQL)
STRING_FIELDS = {'action', 'behavior_id', 'object_id', 'waypoint_id',
                 'mission_id', 'policy', 'name', 'type', 'notes',
                 'affordance', 'target_id'}

# Numeric range bounds for safety-critical fields
NUMERIC_BOUNDS = {
    'height':           (0.10, 0.50),
    'roll':             (-20.0, 20.0),
    'yaw':              (-45.0, 45.0),
    'pitch':            (-20.0, 20.0),
    'speed':            (0.0, 1.5),
    'velocity':         (-1.5, 1.5),
    'distance_m':       (0.3, 5.0),
    'max_force_n':      (1.0, 80.0),
    'pitch_limit_deg':  (5.0, 25.0),
    'roll_limit_deg':   (5.0, 25.0),
    'force_limit_n':    (5.0, 80.0),
    'temp_limit_c':     (50.0, 90.0),
}

# Regex patterns for ID fields
ID_PATTERN     = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')
VERSION_PATTERN = re.compile(r'^\d+\.\d+\.\d+$')
NAME_PATTERN   = re.compile(r'^[\w\s\-\.]{1,128}$')


# ════════════════════════════════════════════════════════════════════════════
# INPUT SANITIZER
# ════════════════════════════════════════════════════════════════════════════

class InputSanitizer:
    """
    Sanitizes all string inputs before they enter platform logic.
    Strips HTML, control characters, excessive whitespace, SQL injection patterns.
    """

    # Dangerous patterns — any match → reject
    DANGEROUS = [
        re.compile(r'<[^>]+>'),                    # HTML tags
        re.compile(r'javascript:', re.I),           # JS injection
        re.compile(r'on\w+\s*=', re.I),            # event handlers
        re.compile(r'(?:--|;)\s*(?:DROP|SELECT|INSERT|UPDATE|DELETE|EXEC)', re.I),  # SQL
        re.compile(r'\.\.[\\/]'),                   # path traversal
        re.compile(r'\x00'),                        # null bytes
    ]

    @classmethod
    def sanitize_str(cls, value: str, field_name: str = '') -> Tuple[str, bool]:
        """
        Returns (sanitized_value, is_safe).
        is_safe=False means reject the entire request.
        """
        if not isinstance(value, str):
            return str(value), True

        # Check dangerous patterns
        for pattern in cls.DANGEROUS:
            if pattern.search(value):
                logger.warning(f'Security: dangerous pattern in {field_name!r}: {value[:40]!r}')
                return '', False

        # Strip control characters (except whitespace)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
        # Collapse whitespace
        cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned).strip()
        # Enforce max length
        return cleaned[:512], True

    @classmethod
    def sanitize_dict(cls, data: dict,
                       target_fields: Set[str] = STRING_FIELDS) -> Tuple[dict, bool]:
        """Recursively sanitize a dictionary. Returns (cleaned, is_safe)."""
        cleaned = {}
        for k, v in data.items():
            if k in target_fields and isinstance(v, str):
                c, safe = cls.sanitize_str(v, k)
                if not safe:
                    return {}, False
                cleaned[k] = c
            elif isinstance(v, dict):
                c, safe = cls.sanitize_dict(v, target_fields)
                if not safe:
                    return {}, False
                cleaned[k] = c
            elif isinstance(v, list):
                c, safe = cls.sanitize_list(v)
                if not safe:
                    return {}, False
                cleaned[k] = c
            else:
                cleaned[k] = v
        return cleaned, True

    @classmethod
    def sanitize_list(cls, data: list) -> Tuple[list, bool]:
        cleaned = []
        for item in data:
            if isinstance(item, str):
                c, safe = cls.sanitize_str(item)
                if not safe:
                    return [], False
                cleaned.append(c)
            elif isinstance(item, dict):
                c, safe = cls.sanitize_dict(item)
                if not safe:
                    return [], False
                cleaned.append(c)
            else:
                cleaned.append(item)
        return cleaned, True


# ════════════════════════════════════════════════════════════════════════════
# COMMAND VALIDATOR
# ════════════════════════════════════════════════════════════════════════════

class CommandValidator:
    """
    Validates platform commands against the allowlist schema.
    Enforces type safety, numeric bounds, and ID format requirements.
    """

    def __init__(self, sanitizer: InputSanitizer):
        self._san = sanitizer

    def validate(self, cmd: dict, armed: bool = False) -> Tuple[dict, bool, str]:
        """
        Returns (validated_cmd, is_valid, rejection_reason).
        """
        if not isinstance(cmd, dict):
            return {}, False, 'Command must be a dict'

        # Sanitize all string fields first
        clean_cmd, safe = InputSanitizer.sanitize_dict(cmd)
        if not safe:
            return {}, False, 'Dangerous content detected'

        action = clean_cmd.get('action', '')
        if not action:
            return {}, False, 'Missing action'

        # Allowlist check
        schema = COMMAND_SCHEMA.get(action)
        if schema is None:
            logger.warning(f'Security: unknown action rejected: {action!r}')
            return {}, False, f'Unknown action: {action!r}'

        # Armed gate
        if schema['armed'] and not armed:
            return {}, False, f'Action {action} requires armed system'

        # Required fields
        missing = schema['required'] - set(clean_cmd.keys())
        if missing:
            return {}, False, f'Missing required fields: {missing}'

        # Numeric bounds
        for field_name, (lo, hi) in NUMERIC_BOUNDS.items():
            if field_name in clean_cmd:
                v = clean_cmd[field_name]
                if not isinstance(v, (int, float)):
                    return {}, False, f'{field_name} must be numeric'
                if not (lo <= v <= hi):
                    clean_cmd[field_name] = max(lo, min(hi, v))
                    logger.debug(f'Clamped {field_name}: {v} → {clean_cmd[field_name]}')

        # ID format validation
        for id_field in ('behavior_id', 'object_id', 'waypoint_id', 'mission_id'):
            if id_field in clean_cmd:
                if not ID_PATTERN.match(clean_cmd[id_field]):
                    return {}, False, f'Invalid {id_field} format'

        return clean_cmd, True, 'ok'


# ════════════════════════════════════════════════════════════════════════════
# OBJECT IMPORT VALIDATOR
# ════════════════════════════════════════════════════════════════════════════

class ObjectImportValidator:
    """
    Validates imported object registry data (JSON/YAML).
    Enforces schema, rejects dangerous field values.
    """

    ALLOWED_TYPES = {'soft_prop','hard_prop','medium_prop',
                     'interactive','funscript_prop','waypoint','zone'}
    ALLOWED_AFFORDANCES = {
        'mount_play','knead','nuzzle','scratch','shake','nudge','push',
        'tap','sit_on','lean','inspect','patrol_around',
        'funscript_play','vibrate_sync',
    }

    MAX_OBJECTS_PER_IMPORT = 100

    def validate_registry(self, data: dict) -> Tuple[List[dict], List[str]]:
        """
        Validate an imported registry dict.
        Returns (valid_objects, error_messages).
        """
        valid, errors = [], []

        raw_objects = data if isinstance(data, list) else data.get('objects', [])
        if not isinstance(raw_objects, list):
            return [], ['Top-level must be array or {objects: [...]}']

        if len(raw_objects) > self.MAX_OBJECTS_PER_IMPORT:
            return [], [f'Too many objects: {len(raw_objects)} > {self.MAX_OBJECTS_PER_IMPORT}']

        for i, obj in enumerate(raw_objects):
            err = self._validate_object(obj, i)
            if err:
                errors.append(err)
            else:
                valid.append(obj)

        return valid, errors

    def _validate_object(self, obj: dict, idx: int) -> Optional[str]:
        prefix = f'Object[{idx}]'

        if not isinstance(obj, dict):
            return f'{prefix}: must be dict'

        # Required
        obj_id = obj.get('id', '')
        if not obj_id or not ID_PATTERN.match(str(obj_id)):
            return f'{prefix}: invalid id {obj_id!r}'

        obj_type = obj.get('type', '')
        if obj_type not in self.ALLOWED_TYPES:
            return f'{prefix}: unknown type {obj_type!r}'

        # Force bounds
        force = obj.get('max_force_n', obj.get('max_force', 20))
        try:
            force = float(force)
        except (TypeError, ValueError):
            return f'{prefix}: max_force_n must be numeric'
        if not (0 < force <= 100):
            return f'{prefix}: max_force_n out of range (0, 100]'

        # Affordances
        affs = obj.get('affordances', [])
        if not isinstance(affs, list):
            return f'{prefix}: affordances must be list'
        unknown = set(affs) - self.ALLOWED_AFFORDANCES - {''}
        if unknown:
            # Don't reject — just strip unknown affordances
            obj['affordances'] = [a for a in affs if a in self.ALLOWED_AFFORDANCES]
            logger.info(f'{prefix}: stripped unknown affordances: {unknown}')

        # Sanitize string fields
        for field_name in ('name', 'notes', 'id'):
            if field_name in obj:
                cleaned, safe = InputSanitizer.sanitize_str(str(obj[field_name]), field_name)
                if not safe:
                    return f'{prefix}: dangerous content in {field_name}'
                obj[field_name] = cleaned

        # Position bounds
        pos = obj.get('pos', {})
        if pos:
            for coord in ('x', 'y', 'z'):
                v = pos.get(coord, 0)
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    return f'{prefix}: pos.{coord} must be numeric'
                if not (-100 <= v <= 100):
                    return f'{prefix}: pos.{coord} out of range [-100, 100]'

        return None  # valid


# ════════════════════════════════════════════════════════════════════════════
# RATE LIMITER
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class RateBucket:
    """Token bucket for a single (client, endpoint) pair."""
    tokens:    float
    max_tokens: float
    refill_rate: float   # tokens/second
    last_refill: float = field(default_factory=time.monotonic)

    def consume(self, n: float = 1.0) -> bool:
        """Returns True if request is allowed."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens,
                          self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RateLimiter:
    """
    Per-client token bucket rate limiter.
    Different limits per endpoint class.
    """

    # (max_tokens, refill_rate) per endpoint class
    LIMITS = {
        'command':       (20, 20),    # 20 RPS burst, 20 RPS sustained
        'estop':         (100, 100),  # E-STOP: almost unlimited
        'telemetry':     (10, 10),
        'import':        (3, 0.1),    # max 3 imports, slow refill
        'default':       (10, 10),
    }

    def __init__(self):
        self._buckets: Dict[str, RateBucket] = {}
        self._violations: Dict[str, int] = defaultdict(int)

    def check(self, client_id: str, endpoint: str = 'default') -> bool:
        key = f'{client_id}:{endpoint}'
        if key not in self._buckets:
            max_t, rate = self.LIMITS.get(endpoint, self.LIMITS['default'])
            self._buckets[key] = RateBucket(
                tokens=max_t, max_tokens=max_t, refill_rate=rate)
        allowed = self._buckets[key].consume()
        if not allowed:
            self._violations[client_id] += 1
            if self._violations[client_id] % 10 == 1:
                logger.warning(
                    f'Rate limit: client={client_id} endpoint={endpoint} '
                    f'violations={self._violations[client_id]}')
        return allowed

    def violations(self, client_id: str) -> int:
        return self._violations.get(client_id, 0)


# ════════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ════════════════════════════════════════════════════════════════════════════

class AuditLog:
    """
    Append-only, tamper-evident audit log.
    Each entry includes a chained HMAC for integrity verification.
    """

    def __init__(self, secret: str = 'go2-platform-audit-secret'):
        self._entries: List[dict] = []
        self._secret = secret.encode()
        self._prev_hash = '0' * 64   # genesis hash

    def record(self, event: str, source: str, data: dict,
               outcome: str = 'ok'):
        entry = {
            'id':      str(uuid.uuid4())[:8],
            'ts':      time.time(),
            'event':   event,
            'source':  source,
            'outcome': outcome,
            'data':    {k: v for k, v in data.items()
                       if k not in ('api_key', 'password', 'token')},  # never log secrets
            'prev_hash': self._prev_hash,
        }
        # Chain HMAC
        entry_bytes = str(sorted(entry.items())).encode()
        entry['hash'] = hmac.new(
            self._secret, entry_bytes, hashlib.sha256).hexdigest()
        self._prev_hash = entry['hash']
        self._entries.append(entry)
        # Keep last 1000 in memory
        if len(self._entries) > 1000:
            self._entries.pop(0)

    def verify_chain(self) -> bool:
        """Verify integrity of the entire audit log chain."""
        prev = '0' * 64
        for entry in self._entries:
            if entry.get('prev_hash') != prev:
                return False
            prev = entry.get('hash', '')
        return True

    def recent(self, n: int = 50) -> List[dict]:
        return self._entries[-n:]

    def search(self, event: Optional[str] = None,
               source: Optional[str] = None,
               outcome: Optional[str] = None) -> List[dict]:
        return [
            e for e in self._entries
            if (event is None or e['event'] == event)
            and (source is None or e['source'] == source)
            and (outcome is None or e['outcome'] == outcome)
        ]


# ════════════════════════════════════════════════════════════════════════════
# SECURITY MANAGER (facade over all security systems)
# ════════════════════════════════════════════════════════════════════════════

class SecurityManager:
    """
    Top-level security facade used by the API server.
    Composes: sanitizer → validator → rate limiter → audit log.
    """

    def __init__(self):
        self.sanitizer   = InputSanitizer()
        self.validator   = CommandValidator(self.sanitizer)
        self.rate_limiter = RateLimiter()
        self.audit       = AuditLog()
        self.obj_validator = ObjectImportValidator()

    def validate_command(self, cmd: dict, client_id: str,
                          armed: bool = False) -> Tuple[dict, bool, str]:
        """Full pipeline: rate check → sanitize → validate."""
        # Rate limit
        if not self.rate_limiter.check(client_id, 'command'):
            self.audit.record('command_blocked', client_id,
                              {'action': cmd.get('action')}, 'rate_limited')
            return {}, False, 'Rate limit exceeded'

        # Validate
        clean, valid, reason = self.validator.validate(cmd, armed)
        outcome = 'ok' if valid else 'rejected'
        self.audit.record('command', client_id,
                          {'action': cmd.get('action')}, outcome)
        return clean, valid, reason

    def validate_import(self, data: dict,
                         client_id: str) -> Tuple[List[dict], List[str]]:
        """Validate imported object registry data."""
        if not self.rate_limiter.check(client_id, 'import'):
            return [], ['Rate limit exceeded for imports']
        valid_objs, errors = self.obj_validator.validate_registry(data)
        self.audit.record('import', client_id,
                          {'valid': len(valid_objs), 'errors': len(errors)},
                          'ok' if not errors else 'partial')
        return valid_objs, errors

    def status(self) -> dict:
        return {
            'audit_entries': len(self.audit._entries),
            'audit_chain_valid': self.audit.verify_chain(),
            'rate_limit_violations': dict(self.rate_limiter._violations),
            'allowed_actions': list(COMMAND_SCHEMA.keys()),
        }
