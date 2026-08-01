"""Adaptive workflow memory from repeated user corrections.

This layer learns higher-level behavior rules, not simple preferences or skill
routes. It is conservative by design: one correction becomes a candidate, and
only repeated corrections from different sessions become formal workflow rules.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta

import yaml

from memory_authority import render_authority_markdown_lines
from memory_schema import (
    MEMORY_RELATION_FIELDS,
    RUNTIME_SCHEMA_VERSION,
    active_formal_lifecycle_metadata,
    canonical_project,
    formal_memory_update_allowed,
    normalize_formal_record,
    upgrade_formal_note_frontmatter,
)
from safety import (
    add_stable_source,
    has_stable_source,
    normalize_project_slug,
    redact_sensitive as redact_sensitive_text,
    safe_vault_path,
    split_frontmatter_text,
    strip_platform_injected_context,
)


CST = timezone(timedelta(hours=8))
DEFAULT_CANDIDATE_DIR = "04-Feedback/_workflow-candidates"
DEFAULT_FORMAL_PATH = "05-Agent-Memory/workflow-rules.md"
DEFAULT_PROMOTE_SEEN_COUNT = 2
DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_INITIAL_CONFIDENCE = 0.58
DEFAULT_REPEAT_INCREMENT = 0.18


def process_workflow_memory(cfg, parsed, project, session_id, date_str):
    settings = workflow_memory_settings(cfg)
    if not settings["enabled"]:
        return empty_result()

    candidates = extract_workflow_candidates(parsed.get("messages", []), project)
    if not candidates:
        return empty_result()

    vault = cfg["vault_path"]
    candidate_dir = safe_vault_path(vault, settings["candidate_dir"])
    formal_path = safe_vault_path(vault, settings["formal_path"])
    os.makedirs(candidate_dir, exist_ok=True)
    os.makedirs(os.path.dirname(formal_path), exist_ok=True)

    existing = load_candidate_records(candidate_dir)
    result = empty_result()
    processed_rule_names = set()

    for candidate in candidates:
        candidate["confidence"] = settings["initial_confidence"]
        rule_name = (
            candidate.get("scope"),
            candidate.get("project"),
            candidate["rule_name"],
        )
        if rule_name in processed_rule_names:
            continue
        processed_rule_names.add(rule_name)

        match = find_similar_candidate(
            candidate,
            existing,
            threshold=settings["similarity_threshold"],
        )
        is_new_source = not match or not has_source(match, session_id, date_str)
        record = merge_candidate(
            candidate=candidate,
            existing=match,
            session_id=session_id,
            date_str=date_str,
            now=datetime.now(CST).isoformat(),
            repeat_increment=settings["repeat_increment"],
        )

        should_promote = int(record.get("seen_count", 0)) >= settings["promote_seen_count"]
        if should_promote:
            already_promoted = record.get("status") == "promoted"
            record["status"] = "promoted"
            path = write_candidate_record(candidate_dir, record)
            if upsert_formal_rule(formal_path, record):
                result["formal"] += 1
            if is_new_source and not already_promoted:
                result["promoted"] += 1
                result["items"].append(result_item(record, "promoted", path, vault))
            elif is_new_source and already_promoted:
                result["updated"] += 1
                result["items"].append(result_item(record, "updated", path, vault))
        else:
            record["status"] = "candidate"
            path = write_candidate_record(candidate_dir, record)
            if is_new_source:
                result["candidates"] += 1
                result["items"].append(result_item(record, "candidate", path, vault))

        existing[record["memory_id"]] = dict(record, _path=path)

    return result


def workflow_memory_settings(cfg):
    raw = cfg.get("workflow_memory") or {}
    return {
        "enabled": raw.get("enabled", True),
        "candidate_dir": raw.get("candidate_dir", DEFAULT_CANDIDATE_DIR),
        "formal_path": raw.get("formal_path", DEFAULT_FORMAL_PATH),
        "promote_seen_count": int(raw.get("promote_seen_count", DEFAULT_PROMOTE_SEEN_COUNT)),
        "similarity_threshold": float(raw.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)),
        "initial_confidence": float(raw.get("initial_confidence", DEFAULT_INITIAL_CONFIDENCE)),
        "repeat_increment": float(raw.get("repeat_increment", DEFAULT_REPEAT_INCREMENT)),
    }


def empty_result():
    return {
        "candidates": 0,
        "promoted": 0,
        "formal": 0,
        "updated": 0,
        "items": [],
    }


def extract_workflow_candidates(messages, project):
    candidates = []
    seen = set()
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        text = strip_platform_injected_context(message.get("text", ""))
        if not text:
            continue
        profile = workflow_profile_for_text(text, project)
        if not profile:
            continue
        if profile["rule_name"] in seen:
            continue
        seen.add(profile["rule_name"])
        profile["message_index"] = index
        candidates.append(profile)
    return candidates


def workflow_profile_for_text(text, project):
    if is_github_source_first_correction(text):
        return profile_github_source_first(text, project)
    if is_pensive_review_then_fix_correction(text):
        return profile_pensive_review_then_fix(text, project)
    return None


def is_github_source_first_correction(text):
    lowered = text.lower()
    has_subject = any(term in lowered for term in ("github", "repo", "repository", "plugin", "skill"))
    has_subject = has_subject or any(term in text for term in ("仓库", "插件", "截图", "项目"))
    has_source = any(term in lowered for term in ("readme", "manifest", "source"))
    has_source = has_source or any(term in text for term in ("源码", "原代码", "原项目", "文档"))
    has_correction = any(term in text for term in ("不要根据名字猜", "不要只看名字", "先去", "先看", "先查", "默认先"))
    return has_subject and has_source and has_correction


def is_pensive_review_then_fix_correction(text):
    negative_probe = str(text or "")
    for positive_phrase in ("不要只报告", "不用只告诉", "不要只告诉", "别停在报告"):
        negative_probe = negative_probe.replace(positive_phrase, "")
    hard_negative = (
        "只读",
        "只审查",
        "仅审查",
        "不要修改",
        "不要改",
        "别修改",
        "只报告",
        "仅报告",
        "先别操作",
        "不要操作",
        "只讨论",
        "仅讨论",
    )
    has_read_only_boundary = any(
        term in negative_probe
        for term in (
            "用户明确要求只读时",
            "用户明确只读时",
            "如果用户要求只读",
            "若用户要求只读",
            "除非用户要求只读",
            "只读模式下",
        )
    )
    if (
        any(term in negative_probe for term in hard_negative)
        and not has_read_only_boundary
    ):
        return False
    lowered = text.lower()
    has_review = "pensive" in lowered or any(term in text for term in ("检查", "审查", "发现问题", "检查出来"))
    has_fix = any(
        term in text
        for term in (
            "直接修复",
            "直接改",
            "继续改",
            "改到测试通过",
            "跑测试",
            "自行改正",
            "自行修复",
            "修改一下",
            "改一下",
            "修复一下",
            "修一下",
            "修复",
        )
    )
    has_fix = has_fix or any(term in text for term in ("不用只告诉", "不要只告诉", "别停在报告"))
    return has_review and has_fix


def profile_github_source_first(text, project):
    return base_profile(
        rule_name="github_source_first",
        title="流程记忆: GitHub 项目先查源码",
        trigger_scene="用户提供 GitHub skill、插件、仓库、项目截图或名称，并要求解释它是什么、是否能用或如何借鉴",
        user_correction="不要根据名字猜，也不要只根据名称猜测用途；先查看 GitHub 原项目 README、源码、manifest 或文档",
        desired_behavior="先打开 upstream GitHub，阅读 README、目录结构、关键源码或 manifest，再给用途、优缺点和可借鉴点结论",
        why_it_matters="GitHub 项目名称和截图容易误导；读取原始资料能避免把包装名、插件名或营销描述误当成实际能力",
        positive_signals=["GitHub", "仓库", "插件", "skill", "截图", "README", "源码", "manifest", "不要根据名字猜"],
        negative_signals=["用户明确说不要联网", "用户只要求根据本地文件分析", "用户只是问通用概念", "网络不可用时先说明限制"],
        evidence=text,
        project=project,
    )


def profile_pensive_review_then_fix(text, project):
    return base_profile(
        rule_name="pensive_review_then_fix",
        title="流程记忆: pensive 审查后直接修复",
        trigger_scene="用户让 pensive 或代码审查流程检查本地项目，发现的是可验证、可测试的代码问题，且目标是完善程序",
        user_correction="不要只报告发现的问题；在可修复且不越权的情况下继续修改并验证",
        desired_behavior="先简要说明关键问题，再直接修复本地代码，运行相关测试；如果涉及越权或破坏性操作则先确认",
        why_it_matters="用户反复把审查结果转成修复任务，停在报告会增加重复沟通成本，也不符合完善程序的目标",
        positive_signals=["pensive", "检查程序", "发现问题", "直接修复", "直接改", "跑测试", "改到测试通过"],
        negative_signals=["用户说先别操作", "用户说只讨论", "用户说只审查", "用户说不要改", "涉及上传 GitHub", "涉及删除文件", "涉及凭据或系统设置"],
        evidence=text,
        project=project,
    )


def base_profile(rule_name, title, trigger_scene, user_correction, desired_behavior,
                 why_it_matters, positive_signals, negative_signals, evidence, project):
    evidence_excerpt = compact_excerpt(redact_sensitive(evidence), 220)
    project = normalize_project_slug(project)
    scope = "project" if project else "global"
    return {
        "memory_id": memory_id_for(rule_name, project=project, scope=scope),
        "status": "candidate",
        "type": "workflow_memory",
        "rule_name": rule_name,
        "title": title,
        "trigger_scene": redact_sensitive(trigger_scene),
        "user_correction": redact_sensitive(user_correction),
        "desired_behavior": redact_sensitive(desired_behavior),
        "why_it_matters": redact_sensitive(why_it_matters),
        "positive_signals": [redact_sensitive(item) for item in positive_signals],
        "negative_signals": [redact_sensitive(item) for item in negative_signals],
        "evidence_excerpt": evidence_excerpt,
        "seen_count": 0,
        "confidence": DEFAULT_INITIAL_CONFIDENCE,
        "source_session": "",
        "project": project,
        "scope": scope,
        "last_seen": "",
        "sources": [],
    }


def merge_candidate(candidate, existing, session_id, date_str, now, repeat_increment):
    if existing:
        record = dict(existing)
        is_new_source = not has_source(record, session_id, date_str)
        if is_new_source:
            record["seen_count"] = int(record.get("seen_count", 0)) + 1
            record["confidence"] = round(
                min(0.95, max(float(record.get("confidence", 0)), candidate["confidence"]) + repeat_increment),
                2,
            )
        else:
            record["seen_count"] = int(record.get("seen_count", 0))
            record["confidence"] = round(max(float(record.get("confidence", 0)), candidate["confidence"]), 2)
        record["last_seen"] = now
        record["source_session"] = session_id
        record["positive_signals"] = merge_unique(record.get("positive_signals", []), candidate.get("positive_signals", []))
        record["negative_signals"] = merge_unique(record.get("negative_signals", []), candidate.get("negative_signals", []))
        if candidate.get("evidence_excerpt") and candidate["evidence_excerpt"] != record.get("evidence_excerpt"):
            examples = record.get("evidence_examples") or [record.get("evidence_excerpt", "")]
            examples.append(candidate["evidence_excerpt"])
            record["evidence_examples"] = list(dict.fromkeys(item for item in examples if item))[-5:]
    else:
        record = dict(candidate)
        record["seen_count"] = 1
        record["first_seen"] = now
        record["last_seen"] = now
        record["source_session"] = session_id

    add_stable_source(record, session_id, date_str)
    return sanitize_record(record)


def find_similar_candidate(candidate, existing, threshold):
    best = None
    best_score = 0.0
    for record in existing.values():
        if candidate_binding(record) != candidate_binding(candidate):
            continue
        if record.get("rule_name") == candidate.get("rule_name"):
            return record
        score = similarity(
            " ".join([candidate.get("trigger_scene", ""), candidate.get("user_correction", "")]),
            " ".join([record.get("trigger_scene", ""), record.get("user_correction", "")]),
        )
        if score > best_score:
            best = record
            best_score = score
    return best if best and best_score >= threshold else None


def has_source(record, session_id, date_str):
    return has_stable_source(record, session_id)


def load_candidate_records(candidate_dir):
    records = {}
    if not os.path.isdir(candidate_dir):
        return records
    for filename in os.listdir(candidate_dir):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(candidate_dir, filename)
        record = read_frontmatter(path)
        if not record:
            continue
        record["_path"] = path
        records[record.get("memory_id") or filename[:-3]] = record
    return records


def read_frontmatter(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return {}
    frontmatter_text, _body = split_frontmatter_text(content)
    if frontmatter_text is None:
        return {}
    try:
        fm = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def write_candidate_record(candidate_dir, record):
    path = record.get("_path")
    if not path:
        stem = safe_filename(record.get("title") or record["memory_id"])
        path = os.path.join(candidate_dir, f"{stem}--{record['memory_id']}.md")
    record = prepare_candidate_record(record)
    write_markdown_with_frontmatter(path, record, render_candidate_body(record))
    return path


def prepare_candidate_record(record):
    record = sanitize_record({k: v for k, v in record.items() if k != "_path"})
    record["schema_version"] = RUNTIME_SCHEMA_VERSION
    record["project"] = canonical_project(record.get("project"))
    record["scope"] = str(
        record.get("scope")
        or ("project" if record.get("project") else "global")
    )
    if record["scope"] == "global":
        record["project"] = ""
    record["status"] = str(record.get("status") or "candidate")
    record["revision"] = candidate_revision(record)
    return record


def candidate_revision(record):
    visible = {
        key: value
        for key, value in record.items()
        if key not in {"revision", "last_seen", "first_seen"}
    }
    return hashlib.sha256(
        json.dumps(visible, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def render_candidate_body(record):
    lines = [
        f"# {record.get('title', record.get('memory_id', 'Workflow memory'))}",
        "",
        record.get("desired_behavior", ""),
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/workflow-rules|Workflow Rules]]",
    ]
    if record.get("project"):
        lines.append(f"- {project_link(record['project'])}")
    lines.extend(
        [
            "",
            "## Workflow Profile",
            "",
            f"- rule_name: `{record.get('rule_name', '')}`",
            f"- trigger_scene: {record.get('trigger_scene', '')}",
            f"- user_correction: {record.get('user_correction', '')}",
            f"- desired_behavior: {record.get('desired_behavior', '')}",
            f"- why_it_matters: {record.get('why_it_matters', '')}",
            f"- positive_signals: {', '.join(f'`{item}`' for item in record.get('positive_signals', []))}",
            f"- negative_signals: {', '.join(f'`{item}`' for item in record.get('negative_signals', []))}",
            "",
            "## Evidence",
            "",
            record.get("evidence_excerpt", ""),
        ]
    )
    if record.get("evidence_examples"):
        lines.extend(["", "## Repeated Evidence", ""])
        lines.extend(f"- {item}" for item in record.get("evidence_examples", []))
    return "\n".join(lines).rstrip() + "\n"


def upsert_formal_rule(formal_path, record):
    existing = ""
    if os.path.exists(formal_path):
        with open(formal_path, "r", encoding="utf-8") as handle:
            existing = handle.read()
        if not formal_memory_update_allowed(existing, record.get("memory_id")):
            return False
    if not existing.strip():
        existing = initial_formal_rules_note()
    before_frontmatter_upgrade = existing
    existing = upgrade_formal_note_frontmatter(
        existing,
        {
            "title": "Workflow Rules",
            "generated_by": "workflow_memory.py",
            "summary_type": "workflow-rules",
        },
    )
    frontmatter_changed = existing != before_frontmatter_upgrade
    has_existing_record = f"- id: `{record['memory_id']}`" in existing
    lifecycle_metadata = {}
    if has_existing_record:
        lifecycle_metadata = active_formal_lifecycle_metadata(
            existing,
            record["memory_id"],
            "workflow",
        )
        if lifecycle_metadata is None:
            return False
    entry = render_formal_rule(record, lifecycle_metadata)
    if has_existing_record:
        updated = replace_formal_rule_entry(existing, record["memory_id"], entry)
        if updated == existing and not frontmatter_changed:
            return False
        write_text_atomic(formal_path, updated.rstrip() + "\n")
        return True
    write_text_atomic(formal_path, existing.rstrip() + "\n\n" + entry.rstrip() + "\n")
    return True


def initial_formal_rules_note():
    return (
        "---\n"
        "title: Workflow Rules\n"
        "generated_by: workflow_memory.py\n"
        "summary_type: workflow-rules\n"
        f"schema_version: '{RUNTIME_SCHEMA_VERSION}'\n"
        "---\n\n"
        "# Workflow Rules\n\n"
        "Promoted workflow rules learned from repeated user corrections.\n\n"
        "## Related\n\n"
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]\n"
        "- [[05-Agent-Memory/personal-memory|Personal Memory]]\n"
        "- [[05-Agent-Memory/skill-routing-rules|Skill Routing Rules]]\n"
        "- [[03-Maps/topic-index|Topic Index]]\n"
    )


def render_formal_rule(record, lifecycle_metadata=None):
    project = canonical_project(record.get("project"))
    formal = normalize_formal_record(
        {
            "id": record.get("memory_id"),
            "title": f"{record.get('rule_name', '')}: {record.get('desired_behavior', '')}",
            "content": record.get("why_it_matters", ""),
            "name": record.get("rule_name", ""),
            "trigger": record.get("trigger_scene", ""),
            "behavior": record.get("desired_behavior", ""),
            "avoid": " ".join(record.get("negative_signals", []) or []),
            "project": project,
            "scope": "project" if project else "global",
            "status": "active",
            "source_refs": [
                f"session:{source_id}"
                for source_id in record.get("source_ids", []) or []
                if source_id
            ],
            **dict(lifecycle_metadata or {}),
        },
        memory_type="workflow",
        default_project=project,
        source_ref=f"candidate:{record.get('memory_id')}",
    )
    source_refs = ", ".join(f"`{item}`" for item in formal["source_refs"])
    lines = [
        f"## {record.get('rule_name', '')}: {record.get('desired_behavior', '')}",
        "",
        f"- id: `{formal['id']}`",
        f"- revision: `{formal['revision']}`",
        "- status: `active`",
    ]
    if formal.get("requires"):
        lines.append(
            "- requires: "
            + ", ".join(f"`{item}`" for item in formal["requires"])
        )
    if formal.get("expires_at"):
        lines.append(f"- expires_at: `{formal['expires_at']}`")
    for key in MEMORY_RELATION_FIELDS:
        if formal.get(key):
            lines.append(
                f"- {key}: "
                + ", ".join(f"`{item}`" for item in formal[key])
            )
    lines.extend(render_authority_markdown_lines(formal))
    lines.extend(
        [
        f"- scope: `{formal['scope']}`",
        f"- rule_name: `{record.get('rule_name', '')}`",
        f"- project: {project_link(formal.get('project', ''))}",
        f"- source_refs: {source_refs}",
        f"- confidence: `{record.get('confidence', '')}`",
        f"- seen_count: `{record.get('seen_count', '')}`",
        f"- source_session: `{record.get('source_session', '')}`",
        f"- last_seen: `{record.get('last_seen', '')}`",
        "",
        "### Trigger scene",
        "",
        record.get("trigger_scene", ""),
        "",
        "### When to apply",
        "",
        ]
    )
    lines.extend(f"- {item}" for item in record.get("positive_signals", []))
    lines.extend(
        [
            "",
            "### Desired behavior",
            "",
            record.get("desired_behavior", ""),
            "",
            "### Why this matters",
            "",
            record.get("why_it_matters", ""),
            "",
            "### Do not apply when",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in record.get("negative_signals", []))
    lines.extend(["", "### Evidence", "", record.get("evidence_excerpt", "")])
    if record.get("evidence_examples"):
        lines.extend(["", "### Repeated Evidence", ""])
        lines.extend(f"- {item}" for item in record.get("evidence_examples", []))
    return "\n".join(lines).rstrip() + "\n"


def replace_formal_rule_entry(existing, memory_id, new_entry):
    heading_pattern = re.compile(r"^##\s+.+$", re.MULTILINE)
    matches = list(heading_pattern.finditer(existing))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(existing)
        section = existing[start:end]
        if f"- id: `{memory_id}`" not in section:
            continue
        return existing[:start].rstrip() + "\n\n" + new_entry.rstrip() + "\n" + existing[end:].lstrip("\n")
    return existing


def write_markdown_with_frontmatter(path, frontmatter, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("---\n")
        yaml.dump(frontmatter, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)
        handle.write("---\n\n")
        handle.write(body)
    os.replace(tmp, path)


def write_text_atomic(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


def result_item(record, action, path, vault):
    rel_path = os.path.relpath(path, vault).replace(os.sep, "/") if path else ""
    return {
        "action": action,
        "rule_name": record.get("rule_name", ""),
        "title": record.get("title", ""),
        "confidence": record.get("confidence", ""),
        "seen_count": record.get("seen_count", ""),
        "path": rel_path,
    }


def sanitize_record(record):
    cleaned = {}
    for key, value in record.items():
        cleaned[key] = sanitize_value(value)
    return cleaned


def sanitize_value(value):
    if isinstance(value, str):
        return redact_sensitive(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def redact_sensitive(text):
    return redact_sensitive_text(text)


def compact_excerpt(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def memory_id_for(rule_name, *, project="", scope=""):
    project, scope = candidate_binding({"project": project, "scope": scope})
    digest = hashlib.sha1(
        f"{scope}:{project}:{rule_name}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:10]
    return f"workflow-{digest}"


def candidate_binding(record):
    project = canonical_project(record.get("project"))
    scope = str(record.get("scope") or ("project" if project else "global"))
    if scope == "global":
        project = ""
    return project, scope


def similarity(left, right):
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def token_set(text):
    text = normalize_for_match(text)
    tokens = set()
    for size in (2, 3):
        tokens.update(text[i : i + size] for i in range(max(len(text) - size + 1, 0)))
    return tokens


def normalize_for_match(text):
    text = str(text or "").lower()
    text = re.sub(r"[`*_\\[\\](){}<>#|,，。.!！?？:：;；\"'“”‘’/\\\\-]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def merge_unique(*lists):
    result = []
    for items in lists:
        for item in items or []:
            if item and item not in result:
                result.append(item)
    return result


def safe_filename(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r'[\\/:*?"<>|#^\[\]]+', "", value)
    value = value.strip(" .")
    return value[:72] or "workflow-memory"


def project_link(project):
    if not project:
        return "`global`"
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"
