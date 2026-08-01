"""Load configuration from config.yaml."""
import os
import re
import sys
import ntpath
import posixpath
import yaml
import json

from safety import assert_no_symlink_components, safe_vault_path
from branding import CODE_PREFIX, PRODUCT_VERSION
from graph_projection import DEFAULT_MAX_NODES, DEFAULT_OUTPUT_DIR

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
RUNTIME_ROOT_DEFAULT = "~/.local/share/agent-memory-beacon/runtime"

MEMORY_RUNTIME_DEFAULTS = {
    'enabled': True,
    'index_path': '05-Agent-Memory/recall-index.json',
    'state_dir': '04-Feedback/_logs/recall-state',
    'log_path': '04-Feedback/_logs/recall-hook.jsonl',
    'hook_timeout_ms': 2000,
    'internal_deadline_ms': 1800,
    'stale_after_minutes': 30,
    'duplicate_suppression_minutes': 60,
    'topic_similarity_threshold': 0.25,
    'topic_min_terms': 3,
    'max_first_prompt': 8,
    'max_refresh': 6,
    'max_risk_or_error': 10,
    'token_budget': 1500,
}

CONVERSATION_SUMMARY_DEFAULTS = {
    "enabled": True,
    "min_substantive_messages": 5,
    "message_interval": 10,
    "stale_after_minutes": 30,
    "retry_interval_messages": 2,
    "max_summary_bytes": 4096,
    "max_recall": 1,
    "token_budget": 400,
}

MEMORY_EFFECTIVENESS_DEFAULTS = {
    'enabled': True,
    'event_log_path': '04-Feedback/_logs/memory-effectiveness.jsonl',
    'report_path': '04-Feedback/memory-effectiveness.md',
    'feedback_window_minutes': 15,
    'max_report_items': 100,
}

MEMORY_PROMOTION_DEFAULTS = {
    'enabled': True,
    'proposal_dir': '04-Feedback/_promotion-proposals',
    'min_source_count': 3,
    'min_exposure_count': 2,
    'max_proposals_per_run': 10,
}

INSIGHT_MEMORY_DEFAULTS = {
    'enabled': True,
    'candidate_dir': '04-Feedback/_insight-candidates',
    'formal_path': '05-Agent-Memory/insights.md',
    'similarity_threshold': 0.58,
    'direct_seed_threshold': 0.72,
    'reinforce_source_count': 2,
    'max_auto_recall': 2,
    'recall_token_budget': 400,
}

ERROR_EVIDENCE_DEFAULTS = {
    'enabled': True,
    'candidate_dir': '04-Feedback/_error-candidates',
    'excerpt_limit': 500,
    'source_limit': 20,
}
ERROR_EVIDENCE_MAX_EXCERPT_LIMIT = 2000
ERROR_EVIDENCE_MAX_SOURCE_LIMIT = 100
GRAPH_PROJECTION_DEFAULTS = {
    'enabled': True,
    'output_dir': DEFAULT_OUTPUT_DIR,
    'max_nodes': DEFAULT_MAX_NODES,
}

ANNOTATION_QUALITY_DEFAULTS = {
    'enabled': True,
    'candidate_dir': '04-Feedback/_annotation-candidates',
    'report_path': '04-Feedback/memory-quality-report.md',
}

MEMORY_LIFECYCLE_DEFAULTS = {
    'proposal_dir': '04-Feedback/_lifecycle-proposals',
    'audit_path': '05-Agent-Memory/lifecycle-audit.md',
    'rollback_dir': '04-Feedback/_rollback/lifecycle',
}

BEACON_SYNC_DEFAULTS = {
    "enabled": False,
    "role": "",
    "device_id": "",
    "state_dir": "",
    "outbox_dir": "",
    "published_dir": "",
    "replica_path": "",
    "received_published_dir": "",
    "inboxes": [],
    "attachment_roots": [],
    "max_chunk_bytes": 16 * 1024 * 1024,
    "max_gap_bytes": 512 * 1024 * 1024,
    "max_attachment_bytes": 32 * 1024 * 1024,
    "max_events_per_run": 32,
    "max_event_json_bytes": 128 * 1024,
    "max_object_bytes": 32 * 1024 * 1024,
    "max_replica_object_bytes": 64 * 1024 * 1024,
    "gc_retention_seconds": 7 * 24 * 60 * 60,
}
BEACON_SYNC_PATH_KEYS = (
    "state_dir",
    "outbox_dir",
    "published_dir",
    "replica_path",
    "received_published_dir",
)
BEACON_SYNC_POSITIVE_LIMITS = (
    "max_chunk_bytes",
    "max_gap_bytes",
    "max_attachment_bytes",
    "max_events_per_run",
    "max_event_json_bytes",
    "max_object_bytes",
    "max_replica_object_bytes",
)
_PERCENT_ENV = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_DOLLAR_ENV = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand(path):
    if not path:
        return path
    return os.path.expandvars(os.path.expanduser(str(path)))

def load_config(path=None):
    with open(path or CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("version", PRODUCT_VERSION)
    cfg.setdefault("product_id", CODE_PREFIX)
    cfg.setdefault('agent', 'codex')
    cfg.setdefault('transcript_agents', ['codex', 'claude', 'zcode'])
    cfg.setdefault('python_path', sys.executable)
    cfg.setdefault('runtime_root', RUNTIME_ROOT_DEFAULT)
    cfg.setdefault('codex_home', os.path.join('~', '.codex'))
    cfg.setdefault('codex_sessions_path', os.path.join(cfg['codex_home'], 'sessions'))
    cfg.setdefault('zcode_home', os.path.join('~', '.zcode'))
    cfg.setdefault('zcode_db_path', os.path.join(cfg['zcode_home'], 'cli', 'db', 'db.sqlite'))
    cfg.setdefault('claude_project_path', '')
    cfg.setdefault('claude_md_path', '')
    cfg.setdefault('context_targets', [])
    cfg.setdefault('agent_memory_path', os.path.join(cfg['vault_path'], '05-Agent-Memory') if cfg.get('vault_path') else '')
    cfg.setdefault('memory_index_path', os.path.join(cfg['vault_path'], '00-Inbox', 'Agent Memory Index.md') if cfg.get('vault_path') else '')
    cfg.setdefault('codex_profile_path', os.path.join(cfg['agent_memory_path'], 'codex-profile') if cfg.get('agent_memory_path') else '')
    cfg.setdefault('codex_profile_check_on_start', True)
    cfg.setdefault('scan_on_start', True)
    cfg.setdefault('harvest_interval_seconds', 300)
    cfg.setdefault('harvest_start_max_transcripts', 32)
    cfg.setdefault('harvest_start_time_budget_seconds', 180)
    cfg.setdefault('privacy', {})
    if not isinstance(cfg['privacy'], dict):
        raise TypeError("config privacy must be a mapping")
    cfg['privacy'].setdefault('store_raw_transcripts', False)
    cfg['privacy'].setdefault('store_transcript_metadata', True)
    cfg['privacy'].setdefault('store_message_samples', False)
    cfg.setdefault('personal_memory', {})
    if not isinstance(cfg['personal_memory'], dict):
        raise TypeError("config personal_memory must be a mapping")
    cfg['personal_memory'].setdefault('enabled', True)
    cfg['personal_memory'].setdefault('candidate_dir', '04-Feedback/_memory-candidates')
    cfg['personal_memory'].setdefault('formal_path', '05-Agent-Memory/personal-memory.md')
    cfg['personal_memory'].setdefault('candidate_threshold', 0.45)
    cfg['personal_memory'].setdefault('direct_threshold', 0.85)
    cfg['personal_memory'].setdefault('promote_seen_count', 2)
    cfg['personal_memory'].setdefault('similarity_threshold', 0.5)
    cfg.setdefault('skill_preferences', {})
    if not isinstance(cfg['skill_preferences'], dict):
        raise TypeError("config skill_preferences must be a mapping")
    cfg['skill_preferences'].setdefault('enabled', True)
    cfg['skill_preferences'].setdefault('candidate_dir', '04-Feedback/_skill-preferences')
    cfg['skill_preferences'].setdefault('formal_path', '05-Agent-Memory/skill-routing-rules.md')
    cfg['skill_preferences'].setdefault('promote_seen_count', 2)
    cfg['skill_preferences'].setdefault('similarity_threshold', 0.5)
    cfg['skill_preferences'].setdefault('initial_confidence', 0.58)
    cfg['skill_preferences'].setdefault('repeat_increment', 0.18)
    cfg.setdefault('workflow_memory', {})
    if not isinstance(cfg['workflow_memory'], dict):
        raise TypeError("config workflow_memory must be a mapping")
    cfg['workflow_memory'].setdefault('enabled', True)
    cfg['workflow_memory'].setdefault('candidate_dir', '04-Feedback/_workflow-candidates')
    cfg['workflow_memory'].setdefault('formal_path', '05-Agent-Memory/workflow-rules.md')
    cfg['workflow_memory'].setdefault('promote_seen_count', 2)
    cfg['workflow_memory'].setdefault('similarity_threshold', 0.5)
    cfg['workflow_memory'].setdefault('initial_confidence', 0.58)
    cfg['workflow_memory'].setdefault('repeat_increment', 0.18)
    _configure_insight_memory(cfg)
    _configure_annotation_quality(cfg)
    _configure_error_evidence(cfg)
    _configure_memory_lifecycle(cfg)
    _configure_memory_effectiveness(cfg)
    _configure_memory_promotion(cfg)
    _configure_memory_runtime(cfg)
    _configure_conversation_summary(cfg)
    _configure_graph_projection(cfg)
    _configure_beacon_sync(cfg)

    for key in [
        'vault_path',
        'claude_project_path',
        'claude_md_path',
        'backup_path',
        'codex_home',
        'codex_sessions_path',
        'zcode_home',
        'zcode_db_path',
        'agent_memory_path',
        'memory_index_path',
        'codex_profile_path',
        'runtime_root',
    ]:
        if key in cfg and cfg[key]:
            cfg[key] = _expand(cfg[key])

    if not os.path.isabs(cfg['runtime_root']):
        raise ValueError("config runtime_root must be an absolute path")

    if cfg.get('transcript_paths'):
        cfg['transcript_paths'] = [_expand(p) for p in cfg.get('transcript_paths', []) if p]

    if not isinstance(cfg.get('context_targets'), list):
        raise TypeError("config context_targets must be a list")
    cfg['context_targets'] = [
        _expand(path) for path in cfg.get('context_targets', []) if path
    ]

    agents = cfg.get('transcript_agents') or [cfg.get('agent', 'codex')]
    if not isinstance(agents, list):
        raise TypeError("config transcript_agents must be a list")
    supported_agents = {'codex', 'claude', 'zcode'}
    cfg['transcript_agents'] = list(dict.fromkeys(
        str(agent).lower() for agent in agents if str(agent).lower() in supported_agents
    ))
    if not cfg['transcript_agents']:
        raise ValueError("config transcript_agents must include codex, claude, or zcode")

    if not cfg.get('log_dir') and cfg.get('vault_path'):
        cfg['log_dir'] = os.path.join(cfg['vault_path'], '04-Feedback', '_logs')

    # Validate required keys
    required = ['vault_path', 'python_path']
    for key in required:
        if key not in cfg:
            raise KeyError(f"config.yaml missing required key: {key}")
    # Validate paths exist
    for key in ['vault_path']:
        if cfg.get(key) and not os.path.exists(cfg[key]):
            raise FileNotFoundError(f"config path not found: {key}={cfg[key]}")

    for section, keys in (
        ('personal_memory', ('candidate_dir', 'formal_path')),
        ('skill_preferences', ('candidate_dir', 'formal_path')),
        ('workflow_memory', ('candidate_dir', 'formal_path')),
        ('insight_memory', ('candidate_dir', 'formal_path')),
        ('annotation_quality', ('candidate_dir', 'report_path')),
        ('error_evidence', ('candidate_dir',)),
        (
            'memory_lifecycle',
            ('proposal_dir', 'audit_path', 'rollback_dir'),
        ),
    ):
        for key in keys:
            configured_path = safe_vault_path(cfg['vault_path'], cfg[section][key])
            if section in {'annotation_quality', 'error_evidence'} and key == 'candidate_dir':
                assert_no_symlink_components(configured_path, cfg['vault_path'])

    # At least one transcript source should exist. It is a warning-level check for
    # setup flows because the user may create it after first launch.
    transcript_roots = [
        cfg.get('codex_sessions_path'),
        cfg.get('zcode_db_path'),
        cfg.get('claude_project_path'),
        *(cfg.get('transcript_paths') or []),
    ]
    if not any(path and os.path.exists(path) for path in transcript_roots):
        print("WARNING: no transcript source exists yet; backup/harvest steps will be no-ops")

    return cfg


def load_beacon_sync_config(path=CONFIG_PATH, *, environ=None):
    """Load only portable producer/replica settings from one YAML file."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise TypeError("config root must be a mapping")
    environment = dict(os.environ)
    if environ is not None:
        if not isinstance(environ, dict):
            raise TypeError("environ must be a mapping")
        environment.update({str(key): str(value) for key, value in environ.items()})
    section = _normalized_beacon_sync(
        raw.get("beacon_sync", {}),
        environment=environment,
        vault_path=raw.get("vault_path", ""),
    )
    transcript_paths = []
    configured_paths = raw.get("transcript_paths", [])
    if configured_paths is None:
        configured_paths = []
    if not isinstance(configured_paths, list):
        raise TypeError("config transcript_paths must be a list")
    for value in (
        raw.get("codex_sessions_path", ""),
        raw.get("claude_project_path", ""),
        *configured_paths,
    ):
        expanded = _expand_portable_path(value, environment)
        if expanded and expanded not in transcript_paths:
            transcript_paths.append(expanded)
    section["transcript_paths"] = transcript_paths
    section["codex_sessions_path"] = _expand_portable_path(
        raw.get("codex_sessions_path", ""),
        environment,
    )
    section["claude_project_path"] = _expand_portable_path(
        raw.get("claude_project_path", ""),
        environment,
    )
    return section


def _configure_beacon_sync(cfg):
    cfg["beacon_sync"] = _normalized_beacon_sync(
        cfg.get("beacon_sync", {}),
        environment=dict(os.environ),
        vault_path=cfg.get("vault_path", ""),
    )


def _normalized_beacon_sync(value, *, environment, vault_path=""):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise TypeError("config beacon_sync must be a mapping")
    unknown = set(value) - set(BEACON_SYNC_DEFAULTS)
    if unknown:
        raise ValueError(
            "beacon_sync contains unexpected keys: " + ", ".join(sorted(unknown))
        )
    settings = {}
    for key, default in BEACON_SYNC_DEFAULTS.items():
        configured = value.get(key, default)
        settings[key] = (
            list(configured)
            if key in {"inboxes", "attachment_roots"}
            and isinstance(configured, list)
            else configured
        )
    if not isinstance(settings["enabled"], bool):
        raise TypeError("beacon_sync.enabled must be a boolean")
    role = settings["role"]
    if not isinstance(role, str) or role not in {"", "authority", "producer-replica"}:
        raise ValueError(
            "beacon_sync.role must be authority or producer-replica"
        )
    if settings["enabled"] and not role:
        raise ValueError("beacon_sync.role is required when enabled")
    device_id = settings["device_id"]
    if not isinstance(device_id, str):
        raise TypeError("beacon_sync.device_id must be a string")
    if device_id and not _valid_sync_device_id(device_id):
        raise ValueError("beacon_sync.device_id is invalid")
    settings["device_id"] = device_id.strip()
    for key in BEACON_SYNC_PATH_KEYS:
        if not isinstance(settings[key], str):
            raise TypeError(f"beacon_sync.{key} must be a string")
        settings[key] = _expand_portable_path(settings[key], environment)
    for key in BEACON_SYNC_POSITIVE_LIMITS:
        configured = settings[key]
        if (
            isinstance(configured, bool)
            or not isinstance(configured, int)
            or configured <= 0
        ):
            raise ValueError(f"beacon_sync.{key} must be a positive integer")
    retention = settings["gc_retention_seconds"]
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 0:
        raise ValueError(
            "beacon_sync.gc_retention_seconds must be a non-negative integer"
        )
    if settings["max_chunk_bytes"] > settings["max_gap_bytes"]:
        raise ValueError(
            "beacon_sync.max_chunk_bytes cannot exceed max_gap_bytes"
        )
    if settings["max_chunk_bytes"] > settings["max_object_bytes"]:
        raise ValueError(
            "beacon_sync.max_chunk_bytes cannot exceed max_object_bytes"
        )
    if settings["max_attachment_bytes"] > settings["max_object_bytes"]:
        raise ValueError(
            "beacon_sync.max_attachment_bytes cannot exceed max_object_bytes"
        )
    if (
        settings["max_attachment_bytes"]
        > settings["max_replica_object_bytes"]
    ):
        raise ValueError(
            "beacon_sync.max_attachment_bytes cannot exceed "
            "max_replica_object_bytes"
        )
    attachment_roots = settings["attachment_roots"]
    if not isinstance(attachment_roots, list):
        raise TypeError("beacon_sync.attachment_roots must be a list")
    normalized_attachment_roots = []
    seen_attachment_roots = set()
    for index, root in enumerate(attachment_roots):
        if not isinstance(root, str):
            raise TypeError("beacon_sync attachment roots must be strings")
        expanded = _expand_portable_path(root, environment)
        if not expanded:
            continue
        style, canonical = _portable_absolute_identity(
            expanded,
            f"beacon_sync.attachment_roots[{index}]",
        )
        identity = (style, canonical)
        if identity in seen_attachment_roots:
            continue
        seen_attachment_roots.add(identity)
        normalized_attachment_roots.append(expanded)
    settings["attachment_roots"] = normalized_attachment_roots
    inboxes = settings["inboxes"]
    if not isinstance(inboxes, list):
        raise TypeError("beacon_sync.inboxes must be a list")
    normalized_inboxes = []
    seen_devices = set()
    for binding in inboxes:
        if not isinstance(binding, dict) or set(binding) != {"device_id", "path"}:
            raise ValueError(
                "each beacon_sync inbox requires device_id and path"
            )
        binding_device = binding["device_id"]
        binding_path = binding["path"]
        if not isinstance(binding_device, str) or not isinstance(binding_path, str):
            raise TypeError("beacon_sync inbox device_id and path must be strings")
        binding_device = binding_device.strip()
        binding_path = _expand_portable_path(binding_path, environment)
        if not binding_path:
            continue
        if not _valid_sync_device_id(binding_device):
            raise ValueError("beacon_sync inbox device_id is invalid")
        if binding_device in seen_devices:
            raise ValueError("beacon_sync inbox device_id must be unique")
        seen_devices.add(binding_device)
        normalized_inboxes.append(
            {"device_id": binding_device, "path": binding_path}
        )
    settings["inboxes"] = normalized_inboxes
    if settings["enabled"]:
        required = ["state_dir"]
        if role == "authority":
            required.append("published_dir")
        else:
            required.extend(
                [
                    "outbox_dir",
                    "received_published_dir",
                    "replica_path",
                ]
            )
            if not settings["device_id"]:
                raise ValueError(
                    "beacon_sync.device_id is required for producer-replica"
                )
        missing = [key for key in required if not settings[key]]
        if missing:
            raise ValueError(
                "enabled beacon_sync paths are empty: " + ", ".join(missing)
            )
    _validate_sync_storage_paths(
        settings,
        vault_path=vault_path,
        environment=environment,
    )
    return settings


def _expand_portable_path(value, environment):
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise TypeError("configured path must be a string")
    text = value.strip()
    if not text:
        return ""

    def replace_percent(match):
        return environment.get(match.group(1), match.group(0))

    def replace_dollar(match):
        name = match.group(1) or match.group(2)
        return environment.get(name, match.group(0))

    text = _PERCENT_ENV.sub(replace_percent, text)
    text = _DOLLAR_ENV.sub(replace_dollar, text)
    if text == "~" or text.startswith(("~/", "~\\")):
        home = environment.get("USERPROFILE") or environment.get("HOME")
        if home:
            text = home + text[1:]
    return text


def _validate_sync_storage_paths(settings, *, vault_path, environment):
    bindings = []
    for key in BEACON_SYNC_PATH_KEYS:
        value = settings.get(key, "")
        if value:
            bindings.append((f"beacon_sync.{key}", value))
    for index, binding in enumerate(settings.get("inboxes") or []):
        value = binding["path"]
        if value:
            bindings.append((f"beacon_sync.inboxes[{index}].path", value))
    for index, value in enumerate(settings.get("attachment_roots") or []):
        if value:
            bindings.append(
                (f"beacon_sync.attachment_roots[{index}]", value)
            )
    if not bindings:
        return

    expanded_vault = _expand_portable_path(vault_path, environment)
    if expanded_vault:
        bindings.append(("vault_path", expanded_vault))

    normalized = []
    for label, value in bindings:
        style, canonical = _portable_absolute_identity(value, label)
        normalized.append((label, style, canonical))
    _reject_sync_path_overlaps(normalized)

    host_style = "windows" if os.name == "nt" else "posix"
    resolved = []
    for label, style, _canonical in normalized:
        if style != host_style:
            continue
        original = next(value for candidate, value in bindings if candidate == label)
        real_path = os.path.realpath(original)
        _resolved_style, real_canonical = _portable_absolute_identity(
            real_path,
            label,
        )
        resolved.append((label, style, real_canonical))
    _reject_sync_path_overlaps(resolved)


def _reject_sync_path_overlaps(normalized):
    for index, (left_label, left_style, left_path) in enumerate(normalized):
        for right_label, right_style, right_path in normalized[index + 1 :]:
            if left_style != right_style:
                continue
            path_module = ntpath if left_style == "windows" else posixpath
            try:
                common = path_module.commonpath((left_path, right_path))
            except ValueError:
                continue
            if common in {left_path, right_path}:
                raise ValueError(
                    "beacon_sync storage paths overlap: "
                    f"{left_label}, {right_label}"
                )


def _portable_absolute_identity(value, label):
    text = str(value or "").strip()
    if _PERCENT_ENV.search(text) or _DOLLAR_ENV.search(text) or (
        text == "~" or text.startswith(("~/", "~\\"))
    ):
        raise ValueError(f"{label} contains an unresolved environment value")
    drive, _tail = ntpath.splitdrive(text)
    windows_style = bool(drive) or text.startswith(("\\\\", "//"))
    if windows_style:
        if not ntpath.isabs(text):
            raise ValueError(f"{label} must be an absolute path")
        return "windows", ntpath.normcase(ntpath.normpath(text))
    if not posixpath.isabs(text):
        raise ValueError(f"{label} must be an absolute path")
    return "posix", posixpath.normpath(text)


def _valid_sync_device_id(value):
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
            str(value or ""),
        )
    )


def _configure_insight_memory(cfg):
    insight = cfg.setdefault('insight_memory', {})
    if not isinstance(insight, dict):
        raise TypeError("config insight_memory must be a mapping")
    for key, value in INSIGHT_MEMORY_DEFAULTS.items():
        insight.setdefault(key, value)
    if not isinstance(insight['enabled'], bool):
        raise TypeError("insight_memory.enabled must be a boolean")
    for key in ('candidate_dir', 'formal_path'):
        value = insight[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"insight_memory.{key} must be a vault-relative path")
    for key in ('similarity_threshold', 'direct_seed_threshold'):
        value = insight[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
        ):
            raise ValueError(f"insight_memory.{key} must be between 0 and 1")
        insight[key] = float(value)
    for key in (
        'reinforce_source_count',
        'max_auto_recall',
        'recall_token_budget',
    ):
        value = insight[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"insight_memory.{key} must be a positive integer")
    if insight['max_auto_recall'] > 2:
        raise ValueError("insight_memory.max_auto_recall cannot exceed 2")
    if insight['recall_token_budget'] > 400:
        raise ValueError("insight_memory.recall_token_budget cannot exceed 400")
    insight['resolved_candidate_dir'] = safe_vault_path(
        cfg['vault_path'], insight['candidate_dir']
    )
    insight['resolved_formal_path'] = safe_vault_path(
        cfg['vault_path'], insight['formal_path']
    )


def _configure_graph_projection(cfg):
    projection = cfg.setdefault('graph_projection', {})
    if not isinstance(projection, dict):
        raise TypeError("config graph_projection must be a mapping")
    for key, value in GRAPH_PROJECTION_DEFAULTS.items():
        projection.setdefault(key, value)
    if not isinstance(projection['enabled'], bool):
        raise TypeError("graph_projection.enabled must be a boolean")
    output_dir = projection['output_dir']
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError(
            "graph_projection.output_dir must be a vault-relative path"
        )
    max_nodes = projection['max_nodes']
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int):
        raise TypeError("graph_projection.max_nodes must be an integer")
    if max_nodes <= 0:
        raise ValueError("graph_projection.max_nodes must be positive")
    projection['resolved_output_dir'] = safe_vault_path(
        cfg['vault_path'],
        output_dir,
    )


def _configure_error_evidence(cfg):
    evidence = cfg.setdefault('error_evidence', {})
    if not isinstance(evidence, dict):
        raise TypeError("config error_evidence must be a mapping")
    for key, value in ERROR_EVIDENCE_DEFAULTS.items():
        evidence.setdefault(key, value)
    if not isinstance(evidence['enabled'], bool):
        raise TypeError("error_evidence.enabled must be a boolean")
    if not isinstance(evidence['candidate_dir'], str) or not evidence['candidate_dir'].strip():
        raise ValueError("error_evidence.candidate_dir must be a vault-relative path")
    for key in ('excerpt_limit', 'source_limit'):
        value = evidence[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"error_evidence.{key} must be a positive integer")
    if evidence['excerpt_limit'] > ERROR_EVIDENCE_MAX_EXCERPT_LIMIT:
        raise ValueError("error_evidence.excerpt_limit exceeds the safety limit")
    if evidence['source_limit'] > ERROR_EVIDENCE_MAX_SOURCE_LIMIT:
        raise ValueError("error_evidence.source_limit exceeds the safety limit")


def _configure_annotation_quality(cfg):
    quality = cfg.setdefault('annotation_quality', {})
    if not isinstance(quality, dict):
        raise TypeError("config annotation_quality must be a mapping")
    for key, value in ANNOTATION_QUALITY_DEFAULTS.items():
        quality.setdefault(key, value)
    if not isinstance(quality['enabled'], bool):
        raise TypeError("annotation_quality.enabled must be a boolean")
    if not isinstance(quality['candidate_dir'], str) or not quality['candidate_dir'].strip():
        raise ValueError(
            "annotation_quality.candidate_dir must be a vault-relative path"
        )


def _configure_memory_lifecycle(cfg):
    lifecycle = cfg.setdefault('memory_lifecycle', {})
    if not isinstance(lifecycle, dict):
        raise TypeError("config memory_lifecycle must be a mapping")
    for key, value in MEMORY_LIFECYCLE_DEFAULTS.items():
        lifecycle.setdefault(key, value)
    for key in MEMORY_LIFECYCLE_DEFAULTS:
        value = lifecycle[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"memory_lifecycle.{key} must be a vault-relative path"
            )
        lifecycle[f"resolved_{key}"] = safe_vault_path(cfg['vault_path'], value)


def _configure_memory_effectiveness(cfg):
    effectiveness = cfg.setdefault('memory_effectiveness', {})
    if not isinstance(effectiveness, dict):
        raise TypeError("config memory_effectiveness must be a mapping")
    for key, value in MEMORY_EFFECTIVENESS_DEFAULTS.items():
        effectiveness.setdefault(key, value)
    if not isinstance(effectiveness['enabled'], bool):
        raise TypeError("memory_effectiveness.enabled must be a boolean")
    for key in ('event_log_path', 'report_path'):
        value = effectiveness[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"memory_effectiveness.{key} must be a vault-relative path"
            )
        effectiveness[f"resolved_{key}"] = safe_vault_path(
            cfg['vault_path'], value
        )
    for key in ('feedback_window_minutes', 'max_report_items'):
        value = effectiveness[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"memory_effectiveness.{key} must be a positive integer"
            )


def _configure_memory_promotion(cfg):
    promotion = cfg.setdefault('memory_promotion', {})
    if not isinstance(promotion, dict):
        raise TypeError("config memory_promotion must be a mapping")
    for key, value in MEMORY_PROMOTION_DEFAULTS.items():
        promotion.setdefault(key, value)
    if not isinstance(promotion['enabled'], bool):
        raise TypeError("memory_promotion.enabled must be a boolean")
    value = promotion['proposal_dir']
    if not isinstance(value, str) or not value.strip():
        raise ValueError("memory_promotion.proposal_dir must be a vault-relative path")
    promotion['resolved_proposal_dir'] = safe_vault_path(cfg['vault_path'], value)
    for key in ('min_source_count', 'min_exposure_count', 'max_proposals_per_run'):
        value = promotion[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"memory_promotion.{key} must be a positive integer")


def _configure_memory_runtime(cfg):
    runtime = cfg.setdefault('memory_runtime', {})
    if not isinstance(runtime, dict):
        raise TypeError("config memory_runtime must be a mapping")
    for key, value in MEMORY_RUNTIME_DEFAULTS.items():
        runtime.setdefault(key, value)

    if not isinstance(runtime['enabled'], bool):
        raise TypeError("memory_runtime.enabled must be a boolean")

    positive_ints = (
        'hook_timeout_ms',
        'internal_deadline_ms',
        'stale_after_minutes',
        'duplicate_suppression_minutes',
        'topic_min_terms',
        'max_first_prompt',
        'max_refresh',
        'max_risk_or_error',
        'token_budget',
    )
    for key in positive_ints:
        value = runtime[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"memory_runtime.{key} must be a positive integer")

    if runtime['hook_timeout_ms'] != 2000:
        raise ValueError("memory_runtime.hook_timeout_ms must be exactly 2000")

    threshold = runtime['topic_similarity_threshold']
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
    ):
        raise ValueError(
            "memory_runtime.topic_similarity_threshold must be between 0 and 1"
        )
    runtime['topic_similarity_threshold'] = float(threshold)

    if runtime['internal_deadline_ms'] >= runtime['hook_timeout_ms']:
        raise ValueError(
            "memory_runtime.internal_deadline_ms must be less than hook_timeout_ms"
        )

    path_fields = (
        ('index_path', 'resolved_index_path'),
        ('state_dir', 'resolved_state_dir'),
        ('log_path', 'resolved_log_path'),
    )
    for source_key, resolved_key in path_fields:
        value = runtime[source_key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"memory_runtime.{source_key} must be a vault-relative path")
        runtime[resolved_key] = safe_vault_path(cfg['vault_path'], value)


def _configure_conversation_summary(cfg):
    summary = cfg.setdefault('conversation_summary', {})
    if not isinstance(summary, dict):
        raise TypeError("config conversation_summary must be a mapping")
    unknown = set(summary) - set(CONVERSATION_SUMMARY_DEFAULTS)
    if unknown:
        raise ValueError(
            "unknown conversation_summary settings: " + ", ".join(sorted(unknown))
        )
    for key, value in CONVERSATION_SUMMARY_DEFAULTS.items():
        summary.setdefault(key, value)

    if not isinstance(summary['enabled'], bool):
        raise TypeError("conversation_summary.enabled must be a boolean")
    for key in (
        'min_substantive_messages',
        'message_interval',
        'stale_after_minutes',
        'retry_interval_messages',
        'max_summary_bytes',
        'max_recall',
        'token_budget',
    ):
        value = summary[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"conversation_summary.{key} must be a positive integer")
    if summary['max_summary_bytes'] > 4096:
        raise ValueError("conversation_summary.max_summary_bytes must not exceed 4096")
    if summary['max_recall'] != 1:
        raise ValueError("conversation_summary.max_recall must be exactly 1")
    if summary['token_budget'] > 400:
        raise ValueError("conversation_summary.token_budget must not exceed 400")
    if summary['retry_interval_messages'] > summary['message_interval']:
        raise ValueError(
            "conversation_summary.retry_interval_messages must not exceed "
            "message_interval"
        )

def get_api_config(cfg):
    """Read API configuration from config.yaml + settings.json.
    Returns {key, base_url, model, temperature, max_tokens, max_retries, retry_backoff_sec}.
    Base URL and model can be overridden in config.yaml; if null, read from settings.json.
    API key lives in settings.json's 'env' block (Claude Code convention).
    """
    api_cfg = cfg.get('api', {})
    settings_path = api_cfg.get('settings_json', '')
    settings = {}
    if settings_path and os.path.exists(settings_path):
        import json
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)

    # API key is nested under 'env' in Claude Code's settings.json
    env = settings.get('env', {})
    api_key = (api_cfg.get('key') or
               env.get('ANTHROPIC_AUTH_TOKEN') or
               settings.get('ANTHROPIC_AUTH_TOKEN') or
               '')

    return {
        'key': api_key,
        'base_url': (api_cfg.get('base_url') or
                     env.get('ANTHROPIC_BASE_URL') or
                     settings.get('ANTHROPIC_BASE_URL') or
                     'https://api.anthropic.com/v1'),
        'model': (api_cfg.get('model') or
                  env.get('ANTHROPIC_MODEL') or
                  settings.get('ANTHROPIC_MODEL') or
                  'claude-sonnet-4-20250514'),
        'temperature': api_cfg.get('temperature', 0.3),
        'max_tokens': api_cfg.get('max_tokens', 4000),
        'max_retries': api_cfg.get('max_retries', 3),
        'retry_backoff_sec': api_cfg.get('retry_backoff_sec', [2, 4, 8]),
    }

def get_api_key(cfg):
    """Read API key from Claude settings.json (not stored in config.yaml).
    DEPRECATED: use get_api_config()['key'] instead.
    """
    return get_api_config(cfg)['key']
