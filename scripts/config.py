"""Load configuration from config.yaml."""
import os
import sys
import yaml
import json

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

def _expand(path):
    if not path:
        return path
    return os.path.expandvars(os.path.expanduser(str(path)))

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault('version', '2.0')
    cfg.setdefault('agent', 'codex')
    cfg.setdefault('python_path', sys.executable)
    cfg.setdefault('codex_home', os.path.join('~', '.codex'))
    cfg.setdefault('codex_sessions_path', os.path.join(cfg['codex_home'], 'sessions'))
    cfg.setdefault('claude_project_path', '')
    cfg.setdefault('claude_md_path', '')
    cfg.setdefault('agent_memory_path', os.path.join(cfg['vault_path'], '05-Agent-Memory') if cfg.get('vault_path') else '')
    cfg.setdefault('memory_index_path', os.path.join(cfg['vault_path'], '00-Inbox', 'Agent Memory Index.md') if cfg.get('vault_path') else '')
    cfg.setdefault('scan_on_start', True)

    for key in [
        'vault_path',
        'claude_project_path',
        'claude_md_path',
        'backup_path',
        'codex_home',
        'codex_sessions_path',
        'agent_memory_path',
        'memory_index_path',
    ]:
        if key in cfg and cfg[key]:
            cfg[key] = _expand(cfg[key])

    if cfg.get('transcript_paths'):
        cfg['transcript_paths'] = [_expand(p) for p in cfg.get('transcript_paths', []) if p]

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

    # At least one transcript source should exist. It is a warning-level check for
    # setup flows because the user may create it after first launch.
    transcript_roots = [
        cfg.get('codex_sessions_path'),
        cfg.get('claude_project_path'),
        *(cfg.get('transcript_paths') or []),
    ]
    if not any(path and os.path.exists(path) for path in transcript_roots):
        print("WARNING: no transcript source exists yet; backup/harvest steps will be no-ops")

    return cfg

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
