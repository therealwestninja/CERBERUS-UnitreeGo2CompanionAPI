"""
go2_platform/backend/i18n/localization.py
══════════════════════════════════════════════════════════════════════════════
Internationalization (i18n) / Localization System

Design principles:
  - All user-facing strings externalized to translation packs (JSON)
  - Runtime language switching without restart
  - Fallback chain: requested → default (en) → key itself
  - ICU MessageFormat-compatible plural / interpolation syntax
  - Thread-safe via asyncio (single event loop assumed)
  - API responses include locale metadata
  - Plugin-contributed translation namespaces

Supported languages (bundled):
  en  — English (default)
  es  — Spanish
  fr  — French
  de  — German
  ja  — Japanese
  zh  — Chinese (Simplified)
  ko  — Korean
  pt  — Portuguese (Brazilian)

Extension:
  Drop a JSON file into backend/i18n/locales/{lang_code}.json
  to add a new language. No code changes required.

Usage:
  from backend.i18n.localization import t, set_locale, get_locale
  msg = t('safety.trip', pitch=10.2, limit=10.0)   # → "Safety trip: Pitch 10.2° > ±10.0°"
  set_locale('ja')
  msg = t('commands.arm')                            # → "アームする"
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger('go2.i18n')

# ── Bundled translation data ──────────────────────────────────────────────
# Inline instead of external files for portability; identical structure to
# what would be loaded from disk.

TRANSLATIONS: Dict[str, Dict[str, Any]] = {

    # ── English (canonical source of truth) ───────────────────────────────
    'en': {
        '_meta': {'name': 'English', 'flag': '🇬🇧', 'rtl': False},
        'app': {
            'title': 'Go2 Companion',
            'subtitle': 'Your Robot Friend',
            'mode_sim': 'Simulation',
            'mode_hw': 'Hardware',
            'mode_hybrid': 'Hybrid',
            'version': 'Version {version}',
        },
        'status': {
            'online': 'Connected',
            'offline': 'Disconnected',
            'connecting': 'Connecting…',
            'armed': 'Armed',
            'disarmed': 'Disarmed',
            'estop': 'Emergency Stop',
            'sim': 'Simulation Active',
        },
        'states': {
            'offline': 'Offline',
            'idle': 'Ready',
            'standing': 'Standing',
            'sitting': 'Sitting',
            'walking': 'Walking',
            'following': 'Following',
            'navigating': 'Navigating',
            'interacting': 'Playing',
            'performing': 'Performing',
            'patrolling': 'Patrolling',
            'fault': 'Needs Attention',
            'estop': 'Stopped',
        },
        'commands': {
            'arm': 'Arm',
            'disarm': 'Disarm',
            'estop': 'Stop',
            'estop_clear': 'Reset Stop',
            'sit': 'Sit',
            'stand': 'Stand',
            'walk': 'Walk',
            'follow': 'Follow Me',
            'navigate': 'Navigate',
            'patrol': 'Patrol',
        },
        'safety': {
            'trip': 'Safety trip: {reason}',
            'pitch': 'Pitch {value}° exceeds limit ±{limit}°',
            'roll': 'Roll {value}° exceeds limit ±{limit}°',
            'force': 'Contact force {value}N exceeds {limit}N',
            'battery': 'Battery critical: {value}%',
            'overtemp': 'Motor {motor} overheating: {value}°C',
            'watchdog': 'Telemetry timeout after {seconds}s',
            'obstacle': 'Obstacle detected at {dist}m',
            'human_zone': 'Human detected in contact zone',
            'estop_active': 'E-STOP active — clear before arming',
            'arm_ok': 'System armed — safety monitoring active',
        },
        'telemetry': {
            'battery': 'Battery',
            'voltage': 'Voltage',
            'pitch': 'Pitch',
            'roll': 'Roll',
            'yaw': 'Yaw',
            'temperature': 'Temperature',
            'force': 'Force',
            'speed': 'Speed',
            'foot_forces': 'Foot Forces',
            'motor_temps': 'Motor Temps',
        },
        'behaviors': {
            'categories': {
                'posture': 'Postures',
                'express': 'Expressions',
                'trick': 'Tricks',
                'play': 'Play',
                'companion': 'Companion',
                'mission': 'Mission',
                'idle': 'Idle',
                'custom': 'Custom',
                'fleet': 'Fleet',
                'navigation': 'Navigation',
            },
        },
        'objects': {
            'title': 'Objects & Props',
            'subtitle': 'Manage what your Go2 interacts with',
            'add': 'Add Object',
            'export': 'Export',
            'import': 'Import',
            'types': {
                'soft_prop': 'Soft Prop',
                'hard_prop': 'Hard Prop',
                'medium_prop': 'Medium Prop',
                'interactive': 'Interactive Device',
                'funscript_prop': 'FunScript Prop',
            },
        },
        'missions': {
            'title': 'Missions',
            'subtitle': 'Autonomous tasks and routes',
            'create': 'New Mission',
            'start': 'Start',
            'stop': 'Stop',
            'types': {
                'patrol': 'Patrol',
                'follow': 'Follow',
                'inspect': 'Inspect',
                'sequence': 'Sequence',
            },
        },
        'settings': {
            'title': 'Settings',
            'connection': 'Connection',
            'safety': 'Safety Limits',
            'appearance': 'Appearance',
            'language': 'Language',
            'ai': 'AI Behavior Generator',
            'simulation': 'Simulation',
            'debug': 'Debug Mode',
            'theme': {
                'warm': 'Warm (default)',
                'light': 'Light',
                'dark': 'Dark',
                'system': 'Follow System',
            },
        },
        'errors': {
            'connection_failed': 'Could not connect to platform backend',
            'auth_required': 'Authentication required',
            'rate_limited': 'Too many requests — slow down',
            'invalid_action': 'Unknown command: {action}',
            'object_not_found': 'Object not found: {id}',
            'arm_required': 'System must be armed first',
            'battery_low': 'Cannot arm — battery too low ({pct}%)',
            'import_failed': 'Import failed: {reason}',
        },
        'animation': {
            'title': 'Animation Studio',
            'load': 'Load Animation',
            'play': 'Play',
            'pause': 'Pause',
            'stop': 'Stop',
            'loop': 'Loop',
            'speed': 'Speed',
            'blend': 'Blend Time',
            'preview': 'Preview',
            'export': 'Export',
            'formats': {
                'funscript': 'FunScript (.funscript)',
                'bvh': 'BVH Motion Capture',
                'json': 'Go2 Native JSON',
                'csv': 'CSV Keyframes',
            },
            'states': {
                'idle': 'No animation loaded',
                'loaded': 'Animation loaded',
                'playing': 'Playing…',
                'paused': 'Paused',
                'blending': 'Blending…',
            },
        },
        'plugins': {
            'title': 'Plugins',
            'activate': 'Activate',
            'deactivate': 'Deactivate',
            'unload': 'Remove',
            'status': {
                'active': 'Active',
                'inactive': 'Inactive',
                'error': 'Error',
            },
        },
        'nav': {
            'home': 'Home',
            'manual': 'Manual',
            'tricks': 'Tricks',
            'objects': 'Objects',
            'missions': 'Missions',
            'animation': 'Animation',
            'settings': 'Settings',
            'minimal': 'Minimal',
        },
    },

    # ── Spanish ───────────────────────────────────────────────────────────
    'es': {
        '_meta': {'name': 'Español', 'flag': '🇪🇸', 'rtl': False},
        'app': {'title': 'Go2 Compañero', 'subtitle': 'Tu Robot Amigo',
                'mode_sim': 'Simulación', 'mode_hw': 'Hardware'},
        'status': {'online': 'Conectado', 'offline': 'Desconectado',
                   'connecting': 'Conectando…', 'armed': 'Armado',
                   'disarmed': 'Desarmado', 'estop': 'Parada de Emergencia'},
        'states': {'idle': 'Listo', 'standing': 'De pie', 'sitting': 'Sentado',
                   'walking': 'Caminando', 'following': 'Siguiendo',
                   'performing': 'Actuando', 'patrolling': 'Patrullando',
                   'fault': 'Necesita Atención', 'estop': 'Detenido'},
        'commands': {'arm': 'Armar', 'disarm': 'Desarmar', 'estop': 'Detener',
                     'sit': 'Sentarse', 'stand': 'Levantarse', 'follow': 'Sígueme'},
        'safety': {'trip': 'Seguridad activada: {reason}',
                   'pitch': 'Cabeceo {value}° supera límite ±{limit}°',
                   'battery': 'Batería crítica: {value}%',
                   'arm_ok': 'Sistema armado — monitoreo de seguridad activo'},
        'nav': {'home': 'Inicio', 'manual': 'Manual', 'tricks': 'Trucos',
                'objects': 'Objetos', 'missions': 'Misiones',
                'animation': 'Animación', 'settings': 'Ajustes'},
        'settings': {'title': 'Ajustes', 'language': 'Idioma',
                     'safety': 'Límites de Seguridad'},
        'errors': {'connection_failed': 'No se pudo conectar al backend',
                   'arm_required': 'El sistema debe estar armado primero'},
    },

    # ── French ────────────────────────────────────────────────────────────
    'fr': {
        '_meta': {'name': 'Français', 'flag': '🇫🇷', 'rtl': False},
        'app': {'title': 'Go2 Compagnon', 'subtitle': 'Votre Ami Robot'},
        'status': {'online': 'Connecté', 'offline': 'Déconnecté',
                   'connecting': 'Connexion…', 'armed': 'Armé',
                   'disarmed': 'Désarmé', 'estop': 'Arrêt d\'Urgence'},
        'states': {'idle': 'Prêt', 'standing': 'Debout', 'sitting': 'Assis',
                   'walking': 'En marche', 'following': 'Suit',
                   'performing': 'Performance', 'patrolling': 'Patrouille',
                   'fault': 'Attention requise', 'estop': 'Arrêté'},
        'commands': {'arm': 'Armer', 'disarm': 'Désarmer', 'estop': 'Arrêter',
                     'sit': 'Assis', 'stand': 'Debout', 'follow': 'Suivez-moi'},
        'safety': {'trip': 'Sécurité déclenchée: {reason}',
                   'battery': 'Batterie critique: {value}%'},
        'nav': {'home': 'Accueil', 'manual': 'Manuel', 'tricks': 'Tours',
                'objects': 'Objets', 'missions': 'Missions',
                'animation': 'Animation', 'settings': 'Paramètres'},
        'settings': {'title': 'Paramètres', 'language': 'Langue'},
    },

    # ── German ────────────────────────────────────────────────────────────
    'de': {
        '_meta': {'name': 'Deutsch', 'flag': '🇩🇪', 'rtl': False},
        'app': {'title': 'Go2 Begleiter', 'subtitle': 'Ihr Roboter-Freund'},
        'status': {'online': 'Verbunden', 'offline': 'Getrennt',
                   'connecting': 'Verbinden…', 'armed': 'Aktiviert',
                   'disarmed': 'Deaktiviert', 'estop': 'Notfall-Stopp'},
        'states': {'idle': 'Bereit', 'standing': 'Stehend', 'sitting': 'Sitzend',
                   'walking': 'Läuft', 'following': 'Folgt',
                   'performing': 'Führt aus', 'patrolling': 'Patrouilliert',
                   'fault': 'Benötigt Aufmerksamkeit', 'estop': 'Gestoppt'},
        'commands': {'arm': 'Aktivieren', 'disarm': 'Deaktivieren',
                     'estop': 'Stopp', 'sit': 'Hinsetzen', 'stand': 'Aufstehen'},
        'nav': {'home': 'Start', 'manual': 'Manuell', 'tricks': 'Tricks',
                'objects': 'Objekte', 'missions': 'Aufgaben',
                'settings': 'Einstellungen'},
        'settings': {'title': 'Einstellungen', 'language': 'Sprache'},
    },

    # ── Japanese ──────────────────────────────────────────────────────────
    'ja': {
        '_meta': {'name': '日本語', 'flag': '🇯🇵', 'rtl': False},
        'app': {'title': 'Go2コンパニオン', 'subtitle': 'あなたのロボット友達'},
        'status': {'online': '接続済み', 'offline': '切断', 'connecting': '接続中…',
                   'armed': 'アーム済み', 'disarmed': 'アーム解除',
                   'estop': '緊急停止'},
        'states': {'idle': '待機中', 'standing': '立っている', 'sitting': '座っている',
                   'walking': '歩いている', 'following': '追跡中',
                   'performing': '実行中', 'patrolling': 'パトロール中',
                   'fault': '注意が必要', 'estop': '停止'},
        'commands': {'arm': 'アーム', 'disarm': 'アーム解除', 'estop': '停止',
                     'sit': '座れ', 'stand': '立て', 'follow': 'ついてきて'},
        'nav': {'home': 'ホーム', 'manual': '手動', 'tricks': 'トリック',
                'objects': 'オブジェクト', 'missions': 'ミッション',
                'settings': '設定'},
        'settings': {'title': '設定', 'language': '言語'},
        'safety': {'trip': 'セーフティトリップ: {reason}',
                   'battery': 'バッテリー残量少: {value}%'},
    },

    # ── Chinese Simplified ────────────────────────────────────────────────
    'zh': {
        '_meta': {'name': '中文（简体）', 'flag': '🇨🇳', 'rtl': False},
        'app': {'title': 'Go2伴侣', 'subtitle': '您的机器人朋友'},
        'status': {'online': '已连接', 'offline': '已断开', 'connecting': '连接中…',
                   'armed': '已激活', 'disarmed': '已停用', 'estop': '紧急停止'},
        'states': {'idle': '就绪', 'standing': '站立', 'sitting': '坐着',
                   'walking': '行走', 'following': '跟随',
                   'performing': '执行中', 'patrolling': '巡逻中',
                   'fault': '需要注意', 'estop': '已停止'},
        'commands': {'arm': '激活', 'disarm': '停用', 'estop': '停止',
                     'sit': '坐下', 'stand': '站起', 'follow': '跟我来'},
        'nav': {'home': '首页', 'manual': '手动', 'tricks': '技巧',
                'objects': '对象', 'missions': '任务', 'settings': '设置'},
        'settings': {'title': '设置', 'language': '语言'},
    },

    # ── Korean ────────────────────────────────────────────────────────────
    'ko': {
        '_meta': {'name': '한국어', 'flag': '🇰🇷', 'rtl': False},
        'app': {'title': 'Go2 컴패니언', 'subtitle': '당신의 로봇 친구'},
        'status': {'online': '연결됨', 'offline': '연결 끊김', 'connecting': '연결 중…',
                   'armed': '활성화됨', 'disarmed': '비활성화됨', 'estop': '비상 정지'},
        'states': {'idle': '준비', 'standing': '서 있음', 'sitting': '앉아 있음',
                   'walking': '걷는 중', 'following': '따라가는 중',
                   'fault': '주의 필요', 'estop': '정지됨'},
        'commands': {'arm': '활성화', 'disarm': '비활성화', 'estop': '정지',
                     'sit': '앉아', 'stand': '일어서'},
        'nav': {'home': '홈', 'manual': '수동', 'tricks': '트릭',
                'settings': '설정'},
        'settings': {'title': '설정', 'language': '언어'},
    },

    # ── Portuguese (Brazilian) ────────────────────────────────────────────
    'pt': {
        '_meta': {'name': 'Português (BR)', 'flag': '🇧🇷', 'rtl': False},
        'app': {'title': 'Go2 Companheiro', 'subtitle': 'Seu Amigo Robô'},
        'status': {'online': 'Conectado', 'offline': 'Desconectado',
                   'connecting': 'Conectando…', 'armed': 'Armado',
                   'disarmed': 'Desarmado', 'estop': 'Parada de Emergência'},
        'states': {'idle': 'Pronto', 'standing': 'Em pé', 'sitting': 'Sentado',
                   'walking': 'Caminhando', 'fault': 'Atenção Necessária',
                   'estop': 'Parado'},
        'commands': {'arm': 'Armar', 'disarm': 'Desarmar', 'estop': 'Parar',
                     'sit': 'Sentar', 'stand': 'Levantar'},
        'nav': {'home': 'Início', 'manual': 'Manual', 'tricks': 'Truques',
                'settings': 'Configurações'},
        'settings': {'title': 'Configurações', 'language': 'Idioma'},
    },
}

# Supported locale codes
SUPPORTED_LOCALES = list(TRANSLATIONS.keys())
DEFAULT_LOCALE = 'en'

# ════════════════════════════════════════════════════════════════════════════
# TRANSLATION ENGINE
# ════════════════════════════════════════════════════════════════════════════

class LocalizationEngine:
    """
    Runtime i18n engine with:
    - Nested key lookup (dot-separated paths like 'safety.pitch')
    - Variable interpolation: t('safety.pitch', value=10.2, limit=10.0)
    - Fallback chain: requested_locale → 'en' → key itself
    - Plugin-contributed namespaces
    - Locale metadata (name, flag, RTL)
    - External JSON locale loading from locales/ directory
    """

    _INTERP_RE = re.compile(r'\{(\w+)\}')

    def __init__(self, default_locale: str = DEFAULT_LOCALE):
        self._locale = default_locale
        self._packs: Dict[str, Dict[str, Any]] = dict(TRANSLATIONS)
        self._plugin_namespaces: Dict[str, Dict[str, Any]] = {}
        self._load_external_locales()
        log.info('i18n engine: %d locales loaded, default=%s',
                 len(self._packs), default_locale)

    def _load_external_locales(self):
        """Load additional locale files from backend/i18n/locales/*.json"""
        locale_dir = Path(__file__).parent / 'locales'
        if not locale_dir.exists():
            return
        for json_file in locale_dir.glob('*.json'):
            lang_code = json_file.stem
            try:
                with open(json_file, encoding='utf-8') as f:
                    data = json.load(f)
                if lang_code in self._packs:
                    self._deep_merge(self._packs[lang_code], data)
                else:
                    self._packs[lang_code] = data
                log.info('i18n: loaded external locale: %s', lang_code)
            except Exception as e:
                log.warning('i18n: failed to load %s: %s', json_file, e)

    def set_locale(self, locale: str) -> bool:
        """Set active locale. Returns True if locale is supported."""
        if locale not in self._packs:
            log.warning('i18n: unsupported locale %r, keeping %s', locale, self._locale)
            return False
        self._locale = locale
        log.info('i18n: locale set to %s (%s)', locale, self.locale_name)
        return True

    @property
    def locale(self) -> str:
        return self._locale

    @property
    def locale_name(self) -> str:
        return self._packs.get(self._locale, {}).get('_meta', {}).get('name', self._locale)

    @property
    def locale_flag(self) -> str:
        return self._packs.get(self._locale, {}).get('_meta', {}).get('flag', '')

    @property
    def is_rtl(self) -> bool:
        return self._packs.get(self._locale, {}).get('_meta', {}).get('rtl', False)

    def translate(self, key: str, locale: Optional[str] = None, **kwargs) -> str:
        """
        Translate a dot-separated key with optional variable interpolation.

        Examples:
          t('states.idle')                    → "Ready"
          t('safety.pitch', value=10.2, limit=10.0) → "Pitch 10.2° exceeds limit ±10.0°"
          t('app.version', version='2.0')     → "Version 2.0"
        """
        active = locale or self._locale
        # Try requested locale, then English fallback, then key itself
        text = self._lookup(active, key) or self._lookup('en', key)
        if text is None:
            log.debug('i18n: missing key %r for locale %r', key, active)
            return key  # Return key as last-resort fallback

        # Variable interpolation
        if kwargs:
            try:
                text = self._INTERP_RE.sub(
                    lambda m: str(kwargs.get(m.group(1), m.group(0))), text)
            except Exception:
                pass  # Return un-interpolated text on error

        return text

    # Convenience alias
    def __call__(self, key: str, locale: Optional[str] = None, **kwargs) -> str:
        return self.translate(key, locale, **kwargs)

    def _lookup(self, locale: str, key: str) -> Optional[str]:
        """Navigate nested dict via dot-separated key path."""
        pack = self._packs.get(locale, {})
        parts = key.split('.')
        node: Any = pack
        for part in parts:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return str(node) if isinstance(node, (str, int, float)) else None

    def available_locales(self) -> List[Dict[str, str]]:
        """Return list of supported locales with metadata."""
        result = []
        for code, pack in self._packs.items():
            meta = pack.get('_meta', {})
            result.append({
                'code': code,
                'name': meta.get('name', code),
                'flag': meta.get('flag', '🌐'),
                'rtl':  str(meta.get('rtl', False)).lower(),
                'is_current': str(code == self._locale).lower(),
            })
        return sorted(result, key=lambda x: x['name'])

    def register_plugin_namespace(self, plugin_name: str,
                                   translations: Dict[str, Any]):
        """
        Plugins can contribute translations under their own namespace.
        Keys accessed as: t(f'{plugin_name}.my_key')
        """
        self._plugin_namespaces[plugin_name] = translations
        for locale_code, pack in self._packs.items():
            if locale_code in translations:
                pack.setdefault(plugin_name, {})
                self._deep_merge(pack[plugin_name], translations[locale_code])
        log.info('i18n: plugin %r registered %d locale(s)',
                 plugin_name, len(translations))

    def export_locale(self, locale: str) -> Dict[str, Any]:
        """Export full translation pack for a locale (for UI download)."""
        return self._packs.get(locale, {})

    def coverage_report(self) -> Dict[str, Any]:
        """Report translation coverage vs English baseline."""
        en_keys = set(self._all_keys(self._packs.get('en', {})))
        report = {}
        for locale, pack in self._packs.items():
            if locale == 'en':
                continue
            locale_keys = set(self._all_keys(pack))
            covered = len(en_keys & locale_keys)
            report[locale] = {
                'total_keys': len(en_keys),
                'translated': covered,
                'coverage_pct': round(100 * covered / max(len(en_keys), 1), 1),
                'missing': sorted(en_keys - locale_keys)[:10],  # first 10 missing
            }
        return report

    @staticmethod
    def _all_keys(d: Dict, prefix: str = '') -> List[str]:
        keys = []
        for k, v in d.items():
            full = f'{prefix}.{k}' if prefix else k
            if k.startswith('_'): continue
            if isinstance(v, dict):
                keys.extend(LocalizationEngine._all_keys(v, full))
            else:
                keys.append(full)
        return keys

    @staticmethod
    def _deep_merge(base: Dict, override: Dict):
        """Merge override into base in-place (nested)."""
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                LocalizationEngine._deep_merge(base[k], v)
            else:
                base[k] = v


# ── Global singleton ──────────────────────────────────────────────────────
_engine = LocalizationEngine()

def t(key: str, locale: Optional[str] = None, **kwargs) -> str:
    """Global translate shorthand."""
    return _engine.translate(key, locale, **kwargs)

def set_locale(locale: str) -> bool:
    return _engine.set_locale(locale)

def get_locale() -> str:
    return _engine.locale

def get_engine() -> LocalizationEngine:
    return _engine


# ════════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES for i18n
# ════════════════════════════════════════════════════════════════════════════

def register_i18n_routes(app, platform=None):
    """Attach i18n endpoints to an existing FastAPI app."""
    from fastapi import Query

    @app.get('/api/v1/i18n/locales', tags=['i18n'])
    async def list_locales():
        """List all available locales with metadata."""
        return {
            'locales': _engine.available_locales(),
            'current': _engine.locale,
            'default': DEFAULT_LOCALE,
        }

    @app.get('/api/v1/i18n/locale', tags=['i18n'])
    async def get_current_locale():
        """Get current active locale."""
        return {
            'locale': _engine.locale,
            'name': _engine.locale_name,
            'flag': _engine.locale_flag,
            'rtl': _engine.is_rtl,
        }

    @app.post('/api/v1/i18n/locale/{locale_code}', tags=['i18n'])
    async def set_locale_endpoint(locale_code: str):
        """Switch active locale at runtime."""
        ok = _engine.set_locale(locale_code)
        if not ok:
            from fastapi import HTTPException
            raise HTTPException(400, detail={
                'error': 'unsupported_locale',
                'supported': [l['code'] for l in _engine.available_locales()],
            })
        # Broadcast to WebSocket clients if platform provided
        if platform:
            await platform.bus.emit('i18n.locale_changed', {
                'locale': locale_code,
                'name': _engine.locale_name,
            }, 'i18n')
        return {'ok': True, 'locale': locale_code, 'name': _engine.locale_name}

    @app.get('/api/v1/i18n/translations/{locale_code}', tags=['i18n'])
    async def get_translations(locale_code: str):
        """Download full translation pack for a locale."""
        pack = _engine.export_locale(locale_code)
        if not pack:
            from fastapi import HTTPException
            raise HTTPException(404, detail=f'Locale not found: {locale_code}')
        return pack

    @app.get('/api/v1/i18n/translate', tags=['i18n'])
    async def translate_key(
        key: str = Query(..., description='Dot-separated translation key'),
        locale: Optional[str] = Query(None),
    ):
        """Translate a single key (useful for testing)."""
        return {'key': key, 'locale': locale or _engine.locale,
                'text': _engine.translate(key, locale)}

    @app.get('/api/v1/i18n/coverage', tags=['i18n'])
    async def coverage():
        """Translation coverage report vs English baseline."""
        return _engine.coverage_report()
