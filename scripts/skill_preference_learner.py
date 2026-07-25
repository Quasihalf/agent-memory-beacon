"""Adaptive skill preference learning from explicit user skill calls.

The learner is intentionally heuristic and conservative. It cannot tune Codex's
internal skill probabilities; it writes Obsidian memory that future sessions can
read as concrete routing guidance.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta

import yaml

from memory_authority import render_authority_markdown_lines
from memory_schema import (
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
DEFAULT_CANDIDATE_DIR = "04-Feedback/_skill-preferences"
DEFAULT_FORMAL_PATH = "05-Agent-Memory/skill-routing-rules.md"
DEFAULT_PROMOTE_SEEN_COUNT = 2
DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_INITIAL_CONFIDENCE = 0.58
DEFAULT_REPEAT_INCREMENT = 0.18


SKILL_ALIASES = {
    "pensive": "pensive",
    "humanizer": "humanizer",
    "manual-memory-capture": "manual-memory-capture",
    "superpowers": "superpowers",
}
NATURAL_LANGUAGE_SKILL_NAMES = {
    "humanizer",
    "manual-memory-capture",
    "pensive",
    "superpowers:systematic-debugging",
    "superpowers:test-driven-development",
}


def process_skill_preferences(cfg, parsed, project, session_id, date_str):
    settings = skill_preference_settings(cfg)
    if not settings["enabled"]:
        return empty_result()

    messages = parsed.get("messages", [])
    invocations = extract_skill_invocations(messages)
    if not invocations:
        return empty_result()

    vault = cfg["vault_path"]
    candidate_dir = safe_vault_path(vault, settings["candidate_dir"])
    formal_path = safe_vault_path(vault, settings["formal_path"])
    os.makedirs(candidate_dir, exist_ok=True)
    os.makedirs(os.path.dirname(formal_path), exist_ok=True)

    existing = load_candidate_records(candidate_dir)
    result = empty_result()
    processed_scene_keys = set()

    for invocation in invocations:
        profile = scene_profile_for(invocation, messages, project, session_id, date_str)
        profile["confidence"] = settings["initial_confidence"]
        scene_key = (
            profile.get("scope"),
            profile.get("project"),
            profile["scene_key"],
        )
        if scene_key in processed_scene_keys:
            continue
        processed_scene_keys.add(scene_key)

        match = find_similar_candidate(
            profile,
            existing,
            threshold=settings["similarity_threshold"],
        )
        is_new_source = not match or not has_source(match, session_id, date_str)
        record = merge_profile(
            profile=profile,
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
            if append_formal_rule(formal_path, record):
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


def empty_result():
    return {
        "candidates": 0,
        "promoted": 0,
        "formal": 0,
        "updated": 0,
        "items": [],
    }


def skill_preference_settings(cfg):
    raw = cfg.get("skill_preferences") or {}
    return {
        "enabled": raw.get("enabled", True),
        "candidate_dir": raw.get("candidate_dir", DEFAULT_CANDIDATE_DIR),
        "formal_path": raw.get("formal_path", DEFAULT_FORMAL_PATH),
        "promote_seen_count": int(raw.get("promote_seen_count", DEFAULT_PROMOTE_SEEN_COUNT)),
        "similarity_threshold": float(raw.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)),
        "initial_confidence": float(raw.get("initial_confidence", DEFAULT_INITIAL_CONFIDENCE)),
        "repeat_increment": float(raw.get("repeat_increment", DEFAULT_REPEAT_INCREMENT)),
    }


def extract_skill_invocations(messages):
    invocations = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        text = str(message.get("text") or "")
        invocation_text = strip_non_invocation_context(text)
        found = []
        found.extend(explicit_dollar_skills(invocation_text))
        found.extend(plugin_link_skills(invocation_text))
        found.extend(at_mention_skills(invocation_text))
        found.extend(natural_language_skills(invocation_text))
        found = [
            item for item in found
            if is_plausible_skill_name(item[0])
            and not is_non_invocation_mention(invocation_text, item[1])
        ]
        for skill_name, raw in dedupe_skill_mentions(found):
            invocations.append(
                {
                    "skill_name": normalize_skill_name(skill_name, invocation_text),
                    "raw": raw,
                    "message_index": index,
                    "message_text": invocation_text,
                }
            )
    return dedupe_invocations(invocations)


def strip_non_invocation_context(text):
    text = strip_platform_injected_context(text)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(">")
    )


def is_non_invocation_mention(text, raw):
    negative_terms = ("不要", "别", "禁止", "无需", "不需要", "避免", "不可")
    example_terms = ("示例", "例如", "比如", "引用", "假设", "example")
    compare_only = any(
        marker in text
        for marker in ("有什么区别", "什么区别", "有何区别", "分别是什么")
    ) and not re.search(r"(?:用|使用|调用|invoke|use)\s+", text, re.IGNORECASE)
    if compare_only:
        return True
    for line in str(text or "").splitlines():
        index = line.find(raw)
        if index < 0:
            continue
        prefix = line[:index].lower()
        if any(term.lower() in prefix for term in example_terms):
            return True
        if any(term in prefix[-24:] for term in negative_terms):
            return True
    return False


def is_plausible_skill_name(value):
    name = str(value or "").strip().strip("$")
    return bool(name and name == name.lower())


def explicit_dollar_skills(text):
    return [
        (match.group(1), match.group(0))
        for match in re.finditer(r"\$([A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z][A-Za-z0-9_-]*)?)", text)
    ]


def plugin_link_skills(text):
    return [
        (match.group(1), match.group(0))
        for match in re.finditer(r"\[@([A-Za-z][A-Za-z0-9_-]*)\]\(plugin://[^\)\s]+\)", text)
    ]


def at_mention_skills(text):
    return [
        (match.group(1), match.group(0))
        for match in re.finditer(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_-]{2,})(?![\w.-])", text)
    ]


def natural_language_skills(text):
    found = []
    for match in re.finditer(
        r"(?:用|使用|调用|invoke|use)\s+\$?([A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z][A-Za-z0-9_-]*)?)",
        text,
        flags=re.IGNORECASE,
    ):
        normalized = normalize_skill_name(match.group(1), text)
        if normalized in NATURAL_LANGUAGE_SKILL_NAMES:
            found.append((normalized, match.group(0)))
    lowered = text.lower()
    if "superpowers" in lowered and any(term in text for term in ("调试", "排查", "debug", "bug")):
        found.append(("superpowers:systematic-debugging", "superpowers 调试 skill"))
    if "superpowers" in lowered and any(term in text for term in ("测试驱动", "TDD", "tdd")):
        found.append(("superpowers:test-driven-development", "superpowers TDD skill"))
    return found


def dedupe_skill_mentions(items):
    seen = set()
    result = []
    for skill_name, raw in items:
        normalized = normalize_skill_name(skill_name)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append((normalized, raw))
    return result


def dedupe_invocations(invocations):
    seen = set()
    result = []
    for invocation in invocations:
        key = (invocation["skill_name"], invocation["message_index"])
        if key in seen:
            continue
        seen.add(key)
        result.append(invocation)
    return result


def normalize_skill_name(skill_name, context=""):
    name = str(skill_name or "").strip().strip("$")
    name = name.replace("：", ":")
    if name == "superpowers":
        if any(term in context for term in ("调试", "排查", "debug", "bug")):
            return "superpowers:systematic-debugging"
    return SKILL_ALIASES.get(name, name)


def scene_profile_for(invocation, messages, project, session_id, date_str):
    skill_name = invocation["skill_name"]
    context = context_for_invocation(invocation, messages)
    base = default_scene_for_skill(skill_name, context)
    evidence = compact_excerpt(redact_sensitive(context), 220)
    positive = merge_unique(base["positive_signals"], signals_from_text(context))
    project = normalize_project_slug(project)
    scope = "project" if project else "global"
    profile = {
        "memory_id": memory_id_for(
            skill_name,
            base["scene_key"],
            project=project,
            scope=scope,
        ),
        "status": "candidate",
        "type": "skill_preference",
        "skill_name": skill_name,
        "scene_key": base["scene_key"],
        "title": f"技能偏好: {skill_name} - {base['short_title']}",
        "task_intent": redact_sensitive(base["task_intent"]),
        "artifact_type": redact_sensitive(base["artifact_type"]),
        "pain_point": redact_sensitive(base["pain_point"]),
        "why_skill_fits": redact_sensitive(base["why_skill_fits"]),
        "positive_signals": [redact_sensitive(item) for item in positive[:8]],
        "negative_signals": [redact_sensitive(item) for item in base["negative_signals"][:8]],
        "evidence_excerpt": evidence,
        "seen_count": 0,
        "confidence": DEFAULT_INITIAL_CONFIDENCE,
        "source_session": session_id,
        "project": project,
        "scope": scope,
        "last_seen": "",
        "sources": [],
    }
    return profile


def context_for_invocation(invocation, messages):
    index = invocation.get("message_index", 0)
    parts = []
    for message in messages[: index + 1]:
        if message.get("role") != "user":
            continue
        text = strip_platform_injected_context(message.get("text", ""))
        if not text:
            continue
        parts.append(text)
    return "\n".join(parts[-3:])


def default_scene_for_skill(skill_name, _context):
    if skill_name == "humanizer":
        return {
            "scene_key": "humanizer_chinese_natural_expression",
            "short_title": "中文表达自然化",
            "task_intent": "把已有中文文本改得更自然、更像真人表达",
            "artifact_type": "中文文本、说明、回复或文档段落",
            "pain_point": "普通改写可能仍保留模板感、AI 味或过度正式的表达",
            "why_skill_fits": "humanizer 专门用于去 AI 味、说人话和保留事实前提下的自然表达",
            "positive_signals": ["自然一点", "说人话", "不像 AI", "去 AI 味", "润色", "模板感"],
            "negative_signals": ["用户只是要求翻译", "用户只是要求压缩字数", "用户要求保留正式学术语气"],
        }
    if skill_name in {
        "systematic-debugging",
        "superpowers:systematic-debugging",
    }:
        return {
            "scene_key": "systematic_debugging_failure_investigation",
            "short_title": "系统化排查错误",
            "task_intent": "排查失败、异常行为或反复出现的 bug",
            "artifact_type": "代码、测试输出、命令错误或运行日志",
            "pain_point": "直接改代码容易只修表面症状，忽略复现、假设验证和回归测试",
            "why_skill_fits": "superpowers:systematic-debugging 要求先定位症状、复现、形成假设并验证修复",
            "positive_signals": ["测试失败", "报错", "bug", "traceback", "行为不符合预期", "排查"],
            "negative_signals": ["用户只是要解释概念", "没有失败症状", "只是新增普通功能且无需调试"],
        }
    if skill_name == "manual-memory-capture":
        return {
            "scene_key": "manual_memory_capture_explicit_record",
            "short_title": "手动记录重点",
            "task_intent": "把用户明确指出的重要内容总结并写入 Obsidian 记忆",
            "artifact_type": "对话重点、用户偏好、项目规则或重要背景",
            "pain_point": "自动记忆判断可能漏掉用户主观认为重要的内容",
            "why_skill_fits": "manual-memory-capture 是显式手动记忆入口，适合用户要求立即记录的场景",
            "positive_signals": ["记一下", "重点记录", "保存到 Obsidian", "手动记录", "这个很重要"],
            "negative_signals": ["内容包含密码或 token", "用户只是临时提醒", "用户不希望长期保存"],
        }
    if skill_name == "pensive" or skill_name.startswith("pensive:"):
        return {
            "scene_key": "pensive_review_requested",
            "short_title": "代码或功能审查",
            "task_intent": "对已有改动、功能或程序进行缺陷和风险检查",
            "artifact_type": "代码 diff、架构改动、测试或程序行为",
            "pain_point": "普通实现反馈可能漏掉边界风险、测试缺口或架构问题",
            "why_skill_fits": "pensive 系列 skill 专门做代码、架构、测试和 bug 审查",
            "positive_signals": ["检查一下", "有没有缺陷", "review", "审查", "风险", "测试缺口"],
            "negative_signals": ["用户只是要实现功能且没有审查需求", "问题不涉及代码或系统设计"],
        }
    return {
        "scene_key": "generic_manual_skill_invocation",
        "short_title": "显式技能调用",
        "task_intent": f"用户明确要求使用 {skill_name} 完成当前任务",
        "artifact_type": "当前对话任务相关内容",
        "pain_point": "普通处理没有主动选择用户认为合适的 skill",
        "why_skill_fits": f"用户手动调用 {skill_name} 表明该 skill 在此类场景中可能更贴合需求",
        "positive_signals": [skill_name, "显式调用", "用这个 skill"],
        "negative_signals": ["只出现一次且没有重复场景证据", "用户只是测试 skill 名称"],
    }


def signals_from_text(text):
    signals = []
    known = [
        "自然一点",
        "说人话",
        "不像 AI",
        "AI味",
        "模板感",
        "重点记录",
        "记一下",
        "检查一下",
        "缺陷",
        "测试失败",
        "报错",
        "bug",
        "上传",
        "Obsidian",
    ]
    for signal in known:
        if signal.lower() in text.lower():
            signals.append(signal)
    return signals


def merge_profile(profile, existing, session_id, date_str, now, repeat_increment):
    if existing:
        record = dict(existing)
        is_new_source = not has_source(record, session_id, date_str)
        if is_new_source:
            record["seen_count"] = int(record.get("seen_count", 0)) + 1
            record["confidence"] = round(
                min(0.95, max(float(record.get("confidence", 0)), profile["confidence"]) + repeat_increment),
                2,
            )
        else:
            record["seen_count"] = int(record.get("seen_count", 0))
            record["confidence"] = round(max(float(record.get("confidence", 0)), profile["confidence"]), 2)
        record["last_seen"] = now
        record["source_session"] = session_id
        record["positive_signals"] = merge_unique(
            record.get("positive_signals", []),
            profile.get("positive_signals", []),
        )
        record["negative_signals"] = merge_unique(
            record.get("negative_signals", []),
            profile.get("negative_signals", []),
        )
        if profile.get("evidence_excerpt") and profile["evidence_excerpt"] != record.get("evidence_excerpt"):
            examples = record.get("evidence_examples") or [record.get("evidence_excerpt", "")]
            examples.append(profile["evidence_excerpt"])
            record["evidence_examples"] = list(dict.fromkeys(item for item in examples if item))[-5:]
    else:
        record = dict(profile)
        record["seen_count"] = 1
        record["first_seen"] = now
        record["last_seen"] = now

    add_stable_source(record, session_id, date_str)
    return sanitize_record(record)


def has_source(record, session_id, date_str):
    return has_stable_source(record, session_id)


def find_similar_candidate(profile, existing, threshold):
    best = None
    best_score = 0.0
    for record in existing.values():
        if candidate_binding(record) != candidate_binding(profile):
            continue
        if record.get("skill_name") != profile.get("skill_name"):
            continue
        if record.get("scene_key") == profile.get("scene_key"):
            return record
        score = similarity(
            " ".join([profile.get("task_intent", ""), profile.get("pain_point", "")]),
            " ".join([record.get("task_intent", ""), record.get("pain_point", "")]),
        )
        if score > best_score:
            best = record
            best_score = score
    return best if best and best_score >= threshold else None


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
    body = render_candidate_body(record)
    write_markdown_with_frontmatter(path, record, body)
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
        f"# {record.get('title', record.get('memory_id', 'Skill preference'))}",
        "",
        record.get("task_intent", ""),
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/skill-routing-rules|Skill Routing Rules]]",
    ]
    if record.get("project"):
        lines.append(f"- {project_link(record['project'])}")
    lines.extend(
        [
            "",
            "## Scene Profile",
            "",
            f"- skill: `{record.get('skill_name', '')}`",
            f"- artifact_type: {record.get('artifact_type', '')}",
            f"- pain_point: {record.get('pain_point', '')}",
            f"- why_skill_fits: {record.get('why_skill_fits', '')}",
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


def append_formal_rule(formal_path, record):
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
            "title": "Skill Routing Rules",
            "generated_by": "skill_preference_learner.py",
            "summary_type": "skill-routing-rules",
        },
    )
    frontmatter_changed = existing != before_frontmatter_upgrade
    has_existing_record = f"- id: `{record['memory_id']}`" in existing
    lifecycle_metadata = {}
    if has_existing_record:
        lifecycle_metadata = active_formal_lifecycle_metadata(
            existing,
            record["memory_id"],
            "skill",
        )
        if lifecycle_metadata is None:
            return False
    entry = render_formal_rule(record, lifecycle_metadata)
    if has_existing_record:
        updated = replace_formal_rule_entry(existing, record["memory_id"], entry)
        if updated == existing and not frontmatter_changed:
            return False
        tmp = formal_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(updated.rstrip() + "\n")
        os.replace(tmp, formal_path)
        return True
    tmp = formal_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(existing.rstrip() + "\n\n" + entry.rstrip() + "\n")
    os.replace(tmp, formal_path)
    return True


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


def initial_formal_rules_note():
    return (
        "---\n"
            "title: Skill Routing Rules\n"
            "generated_by: skill_preference_learner.py\n"
            "summary_type: skill-routing-rules\n"
            f"schema_version: '{RUNTIME_SCHEMA_VERSION}'\n"
            "---\n\n"
        "# Skill Routing Rules\n\n"
        "Promoted rules learned from repeated explicit user skill calls.\n\n"
        "## Related\n\n"
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]\n"
        "- [[05-Agent-Memory/personal-memory|Personal Memory]]\n"
        "- [[03-Maps/topic-index|Topic Index]]\n"
    )


def render_formal_rule(record, lifecycle_metadata=None):
    project = canonical_project(record.get("project"))
    formal = normalize_formal_record(
        {
            "id": record.get("memory_id"),
            "title": f"{record.get('skill_name', '')}: {record.get('task_intent', '')}",
            "content": record.get("why_skill_fits", ""),
            "name": record.get("skill_name", ""),
            "when": " ".join(record.get("positive_signals", []) or []),
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
        memory_type="skill",
        default_project=project,
        source_ref=f"candidate:{record.get('memory_id')}",
    )
    source_refs = ", ".join(f"`{item}`" for item in formal["source_refs"])
    lines = [
        f"## {record.get('skill_name', '')}: {record.get('task_intent', '')}",
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
    lines.extend(render_authority_markdown_lines(formal))
    lines.extend(
        [
        f"- scope: `{formal['scope']}`",
        f"- skill_name: `{record.get('skill_name', '')}`",
        f"- project: {project_link(formal.get('project', ''))}",
        f"- source_refs: {source_refs}",
        f"- confidence: `{record.get('confidence', '')}`",
        f"- seen_count: `{record.get('seen_count', '')}`",
        f"- source_session: `{record.get('source_session', '')}`",
        f"- last_seen: `{record.get('last_seen', '')}`",
        "",
        "### When to consider",
        "",
        ]
    )
    lines.extend(f"- {item}" for item in record.get("positive_signals", []))
    lines.extend(
        [
            "",
            "### Why this skill fits",
            "",
            record.get("why_skill_fits", ""),
            "",
            "### Do not use when",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in record.get("negative_signals", []))
    lines.extend(
        [
            "",
            "### Evidence",
            "",
            record.get("evidence_excerpt", ""),
        ]
    )
    if record.get("evidence_examples"):
        lines.extend(["", "### Repeated Evidence", ""])
        lines.extend(f"- {item}" for item in record.get("evidence_examples", []))
    return "\n".join(lines).rstrip() + "\n"


def write_markdown_with_frontmatter(path, frontmatter, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("---\n")
        yaml.dump(frontmatter, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)
        handle.write("---\n\n")
        handle.write(body)
    os.replace(tmp, path)


def result_item(record, action, path, vault):
    rel_path = os.path.relpath(path, vault).replace(os.sep, "/") if path else ""
    return {
        "action": action,
        "skill_name": record.get("skill_name", ""),
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
    return text[: limit - 1].rstrip() + "…"


def memory_id_for(skill_name, scene_key, *, project="", scope=""):
    project, scope = candidate_binding({"project": project, "scope": scope})
    digest = hashlib.sha1(
        f"{scope}:{project}:{skill_name}:{scene_key}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:10]
    return f"skillpref-{digest}"


def candidate_binding(record):
    project = canonical_project(record.get("project"))
    scope = str(record.get("scope") or ("project" if project else "global"))
    if scope == "global":
        project = ""
    return project, scope


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
    return value[:72] or "skill-preference"


def project_link(project):
    if not project:
        return "`global`"
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"
