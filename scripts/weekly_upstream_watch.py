#!/usr/bin/env python3
"""Weekly upstream watcher for Agent Memory Beacon's upstream fork."""
import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from branding import default_vault_path
from config import CONFIG_PATH, load_config


CST = timezone(timedelta(hours=8))
REPO = Path(__file__).resolve().parents[1]
UPSTREAM_REF = "upstream/master"
LOCAL_REF = "HEAD"
GIT = shutil.which("git") or "/usr/bin/git"


def run_git(args, check=True):
    result = subprocess.run(
        [GIT, *args],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def configured_vault():
    try:
        Path(CONFIG_PATH).lstat()
    except FileNotFoundError:
        return default_vault_path()
    return Path(load_config()["vault_path"]).expanduser()


def watch_paths(vault=None):
    root = Path(vault).expanduser() if vault is not None else configured_vault()
    out_dir = root / "04-Feedback" / "upstream-watch"
    return out_dir, out_dir / "state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state, out_dir, state_path):
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, state_path)


def fetch_upstream():
    run_git(["fetch", "upstream", "--prune"])


def rev(ref):
    return run_git(["rev-parse", ref])


def commit_rows(old_rev, new_rev):
    if old_rev:
        spec = f"{old_rev}..{new_rev}"
    else:
        spec = f"{LOCAL_REF}..{new_rev}"
    raw = run_git(["log", "--date=short", "--pretty=format:%h%x09%ad%x09%s", spec], check=False)
    rows = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return rows


def diff_stat(old_rev, new_rev):
    base = old_rev or LOCAL_REF
    return run_git(["diff", "--stat", f"{base}..{new_rev}"], check=False)


def changed_files(old_rev, new_rev):
    base = old_rev or LOCAL_REF
    raw = run_git(["diff", "--name-status", f"{base}..{new_rev}"], check=False)
    return [line for line in raw.splitlines() if line.strip()]


def categorize(rows, files):
    text = "\n".join([row["subject"] for row in rows] + files).lower()
    features = []
    if "global_atoms" in text or "atom" in text:
        features.append("跨项目 global atoms：把多个项目重复出现的错误提炼成全局经验。")
    if "keyword_index" in text or "keyword" in text:
        features.append("关键词索引：为 Agent 提供更直接的历史知识检索入口。")
    if "pre_action" in text or "pre-action" in text:
        features.append("行动前触发：在 Agent 开始写代码前注入必须检查的知识规则。")
    if "session_start" in text or "session_close" in text:
        features.append("会话启动/收尾协议：把 T1/T2 流程拆成更明确的脚本。")
    if "install.py" in text:
        features.append("统一安装器：用一个 install.py 替代多个平台安装脚本。")
    if "readme" in text or "changelog" in text or ".github" in text:
        features.append("文档和项目治理：更新 README/CHANGELOG/GitHub 模板。")
    if not features:
        features.append("本次更新主要是小修、文档或结构整理，需要人工进一步判断是否值得融合。")
    return features


def compare_notes(features, files):
    advantages = []
    risks = []
    if any("global atoms" in item for item in features):
        advantages.append("可借鉴跨项目经验沉淀，适合补强当前 personal-memory 之外的通用错误知识。")
    if any("关键词索引" in item for item in features):
        advantages.append("可提升 Codex 检索历史 session/decision/error 的效率。")
    if any("行动前触发" in item for item in features):
        advantages.append("可减少“记了但新会话没读到”的问题。")
    if any("统一安装器" in item for item in features):
        advantages.append("安装路径更统一，但需要评估是否兼容当前 macOS Codex/Claude 自动收割。")
    if any(line.startswith(("D\t", "M\t")) and "session_harvester.py" in line for line in files):
        risks.append("上游改动触及 session_harvester.py，直接合并可能覆盖当前 Obsidian vault 自动化和图谱补链逻辑。")
    if any(line.startswith("D\t") and ("install_codex.py" in line or "install_claude.py" in line) for line in files):
        risks.append("上游删除平台安装脚本，可能和当前分别适配 Codex/Claude 的策略冲突。")
    if any(".claude" in item.lower() for item in features) or any("project-local" in item.lower() for item in features):
        risks.append("上游更偏 project-local 存储，和当前以配置的 Obsidian Vault 为中心的设计不同。")
    if not advantages:
        advantages.append("可作为上游变化参考，暂不一定需要融合。")
    if not risks:
        risks.append("未发现明显结构性冲突；仍建议 cherry-pick，而不是直接 pull。")
    return advantages, risks


def render_report(old_rev, new_rev, rows, files, stat):
    now = datetime.now(CST)
    features = categorize(rows, files)
    advantages, risks = compare_notes(features, files)
    lines = [
        "---",
        f"title: Upstream Watch {now.strftime('%Y-%m-%d')}",
        f"date: {now.strftime('%Y-%m-%d')}",
        f"old_rev: {old_rev or ''}",
        f"new_rev: {new_rev}",
        f"commit_count: {len(rows)}",
        "generated_by: weekly_upstream_watch.py",
        "---",
        "",
        f"# Upstream Watch {now.strftime('%Y-%m-%d')}",
        "",
        "## Summary",
        "",
        f"上游 `{UPSTREAM_REF}` 有更新：`{old_rev or 'local HEAD'}` → `{new_rev}`。",
        "",
        "## New Commits",
        "",
        "| Date | Commit | Subject |",
        "|---|---|---|",
    ]
    for row in rows[:30]:
        lines.append(f"| {row['date']} | `{row['sha']}` | {escape_cell(row['subject'])} |")
    lines.extend(["", "## New/Changed Features", ""])
    lines.extend(f"- {item}" for item in features)
    lines.extend(["", "## Compared With Current Personalized Version", "", "### Pros / 可借鉴", ""])
    lines.extend(f"- {item}" for item in advantages)
    lines.extend(["", "### Cons / 风险", ""])
    lines.extend(f"- {item}" for item in risks)
    lines.extend(["", "## Changed Files", ""])
    lines.extend(f"- `{line}`" for line in files[:80])
    lines.extend(["", "## Diff Stat", "", "```text", stat or "(no diff stat)", "```", ""])
    return "\n".join(lines)


def escape_cell(value):
    return str(value).replace("|", "\\|")


def write_report(content, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(CST).strftime("%Y-%m-%d")
    path = out_dir / f"{date}-upstream-update.md"
    path.write_text(content, encoding="utf-8")
    return path


def main():
    out_dir, state_path = watch_paths()
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="Record current upstream as baseline.")
    parser.add_argument("--force-report", action="store_true", help="Write a report even if no new upstream rev.")
    args = parser.parse_args()

    fetch_upstream()
    current = rev(UPSTREAM_REF)
    state = load_state(state_path)
    previous = state.get("last_seen_upstream")

    if args.init:
        save_state({
            **state,
            "last_seen_upstream": current,
            "last_checked": datetime.now(CST).isoformat(),
        }, out_dir, state_path)
        print(f"[upstream-watch] baseline set to {current}")
        return 0

    if previous == current and not args.force_report:
        state["last_checked"] = datetime.now(CST).isoformat()
        save_state(state, out_dir, state_path)
        print(f"[upstream-watch] no update: {current}")
        return 0

    rows = commit_rows(previous, current)
    files = changed_files(previous, current)
    stat = diff_stat(previous, current)
    report = render_report(previous, current, rows, files, stat)
    path = write_report(report, out_dir)
    save_state({
        **state,
        "last_seen_upstream": current,
        "last_report": str(path),
        "last_checked": datetime.now(CST).isoformat(),
    }, out_dir, state_path)
    print(f"[upstream-watch] wrote report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
