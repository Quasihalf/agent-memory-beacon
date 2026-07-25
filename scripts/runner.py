#!/usr/bin/env python3
"""Agent Memory Beacon weekly scanner pipeline orchestrator."""
import sys
import os
import json
import yaml
import argparse
import tempfile
from datetime import datetime, timezone
from config import load_config
from safety import split_frontmatter_text

# Force UTF-8 on Windows — otherwise emoji and Chinese crash GBK stdout
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

STEPS = ["backup", "analyze", "maintain", "report", "compile"]
LOCK_FILE = "scanner.lock"

def main():
    parser = argparse.ArgumentParser(description="Agent Memory Beacon weekly scanner")
    parser.add_argument("--dry-run", action="store_true", help="Analyze without modifying files")
    parser.add_argument("--full", action="store_true", help="Full rescan of all sessions")
    parser.add_argument("--rollback", metavar="DATE", help="Rollback to pre-scan state (format: YYYY-MM-DD)")
    parser.add_argument("--step", choices=STEPS, help="Run single step only")
    parser.add_argument("--force", action="store_true", help="Force run even if another scan is in progress")
    args = parser.parse_args()

    cfg = load_config()
    log_dir = cfg.get('log_dir', os.path.join(cfg['vault_path'], '04-Feedback', '_logs'))
    logger = ScanLogger(log_dir)

    # Vault version compatibility check
    vault_readme = os.path.join(cfg['vault_path'], 'README.md')
    if os.path.exists(vault_readme):
        with open(vault_readme, 'r', encoding='utf-8') as f:
            content = f.read()
        frontmatter_text, _body = split_frontmatter_text(content)
        if frontmatter_text is not None:
            try:
                fm = yaml.safe_load(frontmatter_text)
                vault_ver = fm.get('vault_version', '0.0')
                script_ver = "2.0"
                if int(str(vault_ver).split('.')[0]) > int(script_ver.split('.')[0]):
                    print(f"ERROR: Vault version {vault_ver} is incompatible with script version {script_ver}")
                    print("Please upgrade the scanner scripts or downgrade the vault.")
                    sys.exit(1)
            except Exception:
                pass  # If we can't parse the version, proceed with caution

    # Missed-scan catch-up: if last scan was > 7 days ago, auto-enable full mode
    # 漏扫描补跑：如果上次扫描超过7天，自动开启全量模式
    heartbeat_path = os.path.join(cfg['vault_path'], '04-Feedback', 'heartbeat.md')
    args.full, missed_weeks = resolve_scan_mode(
        heartbeat_path,
        requested_full=args.full,
    )
    if missed_weeks:
        print(f"⚠ 约 {missed_weeks} 周未扫描 / Approximately {missed_weeks} week(s) missed")
        print("  自动启用全量模式，补跑积压 session / Auto-enabling full mode to catch up")

    # Scan overlap prevention
    lock_path = os.path.join(cfg['vault_path'], '04-Feedback', LOCK_FILE)
    if not acquire_lock(lock_path, force=args.force):
        print("ERROR: Another scan is in progress. Use --force to override.")
        sys.exit(1)

    try:
        logger.log("runner_start", {
            "dry_run": args.dry_run,
            "full": args.full,
            "step": args.step,
            "rollback": args.rollback,
            "missed_weeks": missed_weeks
        })
        print(f"[{datetime.now().isoformat()}] Runner starting (dry-run={args.dry_run}, full={args.full}, missed_weeks={missed_weeks})")

        if args.rollback:
            rollback(cfg, args.rollback)
            return

        # Step execution with independent error handling
        steps_to_run = [args.step] if args.step else STEPS
        results = {}

        for step in steps_to_run:
            try:
                print(f"  Step: {step}...")
                logger.log("step_start", {"step": step})
                # Each step is a module import + run
                if step == "backup":
                    from backup import run as backup_run
                    results[step] = backup_run(cfg, dry_run=args.dry_run, full=args.full)
                elif step == "analyze":
                    from analyzer import run as analyze_run
                    results[step] = analyze_run(cfg, dry_run=args.dry_run, full=args.full)
                elif step == "maintain":
                    from maintainer import run as maintain_run
                    results[step] = maintain_run(cfg, dry_run=args.dry_run, step_results=results)
                elif step == "report":
                    from reporter import run as report_run
                    results[step] = report_run(cfg, dry_run=args.dry_run, step_results=results, missed_weeks=missed_weeks)
                elif step == "compile":
                    from compiler import run as compile_run
                    results[step] = compile_run(cfg, dry_run=args.dry_run, step_results=results)
                if result_has_errors(results[step]):
                    print(f"    WARN: {results[step]}")
                    logger.log(
                        "step_error",
                        {"step": step, "error": str(results[step])[:500]},
                    )
                else:
                    print(f"    OK: {results[step]}")
                    logger.log("step_complete", {"step": step, "result_summary": str(results[step])[:200]})
            except Exception as e:
                print(f"    FAIL: {e}")
                results[step] = {"error": str(e)}
                logger.log("step_error", {"step": step, "error": str(e)})
                # Continue to next step (don't abort pipeline)

        print(f"[{datetime.now().isoformat()}] Runner complete")
        logger.log("runner_complete", {"steps_completed": list(results.keys())})
        cleanup_old_logs(log_dir)
        return results
    finally:
        release_lock(lock_path)


def resolve_scan_mode(heartbeat_path, requested_full=False, now=None):
    """Return whether this run is full and how many whole weeks were missed."""
    if requested_full:
        return True, 0
    if not os.path.exists(heartbeat_path):
        return False, 0

    try:
        with open(heartbeat_path, 'r', encoding='utf-8') as handle:
            heartbeat = handle.read()
        frontmatter_text, _body = split_frontmatter_text(heartbeat)
        if frontmatter_text is None:
            return False, 0
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, 0
        last_scan_value = frontmatter.get('last_scan')
        if not last_scan_value or str(last_scan_value) == 'null':
            return False, 0
        if isinstance(last_scan_value, datetime):
            last_scan = last_scan_value
        else:
            last_scan = datetime.fromisoformat(str(last_scan_value))
        current = now or datetime.now()
        days_since = (
            _as_utc(current) - _as_utc(last_scan)
        ).days
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return False, 0

    if days_since <= 7:
        return False, 0
    return True, days_since // 7


def _as_utc(value):
    """Normalize aware or local-naive datetimes before elapsed-time checks."""
    if not isinstance(value, datetime):
        raise TypeError("scan timestamp must be a datetime")
    return value.astimezone(timezone.utc)

def acquire_lock(lock_path, force=False):
    """Prevent concurrent scans. Returns True if lock acquired."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                age = datetime.now().timestamp() - os.path.getmtime(lock_path)
            except OSError:
                continue
            if not force and age <= 7200:
                return False
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                continue
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({
                'pid': os.getpid(),
                'created_at': datetime.now().isoformat(),
            }))
            handle.write('\n')
        return True
    return False

def release_lock(lock_path):
    """Release the scan lock."""
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass


def result_has_errors(result):
    if not isinstance(result, dict):
        return False
    return any(
        value and (key == 'error' or key.endswith('_error'))
        for key, value in result.items()
    )


def results_have_errors(results):
    return any(result_has_errors(result) for result in (results or {}).values())

def rollback(cfg, date_str):
    """Restore files from _rollback/{date}/ to vault."""
    import shutil
    rollback_dir = os.path.join(cfg['vault_path'], '04-Feedback', '_rollback', date_str)
    if not os.path.exists(rollback_dir):
        print(f"No rollback data for {date_str}")
        return
    vault = cfg['vault_path']
    for root, dirs, files in os.walk(rollback_dir):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, rollback_dir)
            dst = os.path.join(vault, rel)
            shutil.copy2(src, dst)
            print(f"  Restored: {rel}")
    print(f"Rollback complete: {date_str}")


class ScanLogger:
    """JSON Lines structured logger with module-level log files."""

    def __init__(self, log_dir):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.date_str = datetime.now().strftime('%Y-%m-%d')

    def log(self, module, data):
        """Write a JSON Lines log entry to {module}-{date}.log."""
        log_path = os.path.join(self.log_dir, f"{module}-{self.date_str}.log")
        entry = {
            "timestamp": datetime.now().isoformat(),
            "module": module,
            **data
        }
        try:
            # Atomic append: write to tmp, then append-rename is not needed for JSONL
            # since each line is independent. Use a simple append with newline.
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception:
            pass  # Logging failure should not crash the pipeline


def cleanup_old_logs(log_dir, retention_days=30):
    """Delete log files older than retention_days."""
    if not os.path.exists(log_dir):
        return
    cutoff = datetime.now().timestamp() - (retention_days * 86400)
    deleted = 0
    for f in os.listdir(log_dir):
        if f.endswith('.log'):
            fp = os.path.join(log_dir, f)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    deleted += 1
            except OSError:
                pass
    if deleted:
        print(f"  Log cleanup: removed {deleted} old log files")


if __name__ == '__main__':
    pipeline_results = main()
    if isinstance(pipeline_results, dict) and results_have_errors(pipeline_results):
        sys.exit(1)
