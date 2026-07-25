"""Heuristic personal-memory extraction for harvested agent transcripts.

The judge is intentionally conservative. It promotes obvious long-term
preferences directly and keeps weaker signals in a candidate folder until they
repeat across sessions.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta

import yaml

from annotation_quality import QUALITY_FORMAL, QUALITY_REJECTED, assess_favor
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
    normalize_project_slug as safe_project_slug,
    redact_sensitive,
    safe_vault_path,
    split_frontmatter_text,
    strip_markdown_code_blocks,
    strip_platform_injected_context,
)


CST = timezone(timedelta(hours=8))
EXPLICIT_FAVOR_CONFIDENCE = 0.9
MEMORY_TYPES = {"preference", "project_rule", "environment"}

PREFERENCE_PATTERNS = [
    "我想",
    "我希望",
    "我的想法",
    "我的需求",
    "我的目的",
    "我更喜欢",
    "我不想",
    "不要",
    "以后",
    "默认",
    "优先",
    "保留",
    "自动",
    "个性化",
    "待确认",
    "重复",
]

NOISE_PATTERNS = [
    "现在怎么样",
    "继续",
    "可以",
    "好的",
    "谢谢",
    "怎么确认",
    "讲简单",
    "有什么区别",
    "什么区别",
    "为什么",
    "怎么",
]

PROJECT_HINT_PATTERNS = [
    "程序",
    "项目",
    "codex",
    "claude",
    "zcode",
    "obsidian",
    "github",
    "vault",
    "sqlite",
    "记录",
    "自动化",
]


def process_personal_memory(cfg, parsed, project, session_id, date_str):
    """Extract candidate personal memories and write/promote them.

    Returns a summary dict with candidate/promoted/formal counts.
    """
    settings = memory_settings(cfg)
    if not settings["enabled"]:
        return empty_result()

    candidates = extract_memory_candidates(
        parsed.get("messages", []),
        project,
        settings["candidate_threshold"],
    )
    if not candidates:
        return empty_result()

    vault = cfg["vault_path"]
    candidate_dir = safe_vault_path(vault, settings["candidate_dir"])
    formal_path = safe_vault_path(vault, settings["formal_path"])
    os.makedirs(candidate_dir, exist_ok=True)
    os.makedirs(os.path.dirname(formal_path), exist_ok=True)

    existing = load_candidate_records(candidate_dir)
    result = empty_result()
    now = datetime.now(CST).isoformat()

    for candidate in candidates:
        match = find_similar_candidate(candidate, existing, settings["similarity_threshold"])
        is_new_source = not match or not has_source(match, session_id, date_str)
        record = merge_candidate(
            candidate=candidate,
            existing=match,
            session_id=session_id,
            date_str=date_str,
            now=now,
        )
        should_promote = (
            bool(record.get("explicit"))
            and record["confidence"] >= settings["direct_threshold"]
        ) or record["seen_count"] >= settings["promote_seen_count"]
        if should_promote:
            already_promoted = record.get("status") == "promoted"
            if append_formal_memory(formal_path, record):
                result["formal"] += 1
            if not is_new_source:
                action = None
            elif not already_promoted:
                result["promoted"] += 1
                action = "promoted"
            else:
                result["updated"] += 1
                action = "updated"
            record["status"] = "promoted"
            path = write_candidate_record(candidate_dir, record)
        else:
            record["status"] = "candidate"
            path = write_candidate_record(candidate_dir, record)
            if is_new_source:
                result["candidates"] += 1
                action = "candidate"
            else:
                action = None
        if action:
            result["items"].append(memory_result_item(record, action, path, vault))
        existing[record["memory_id"]] = record

    return result


def empty_result():
    return {
        "candidates": 0,
        "promoted": 0,
        "formal": 0,
        "updated": 0,
        "items": [],
    }


def memory_result_item(record, action, path, vault):
    rel_path = ""
    if path:
        rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
    return {
        "action": action,
        "title": record.get("title", record.get("memory_id", "")),
        "content": record.get("content", ""),
        "confidence": record.get("confidence", ""),
        "seen_count": record.get("seen_count", ""),
        "path": rel_path,
    }


def memory_settings(cfg):
    raw = cfg.get("personal_memory") or {}
    return {
        "enabled": raw.get("enabled", True),
        "candidate_dir": raw.get(
            "candidate_dir", "04-Feedback/_memory-candidates"
        ),
        "formal_path": raw.get(
            "formal_path", "05-Agent-Memory/personal-memory.md"
        ),
        "candidate_threshold": float(raw.get("candidate_threshold", 0.45)),
        "direct_threshold": float(raw.get("direct_threshold", 0.85)),
        "promote_seen_count": int(raw.get("promote_seen_count", 2)),
        "similarity_threshold": float(raw.get("similarity_threshold", 0.5)),
    }


def extract_memory_candidates(messages, project, threshold=0.45):
    candidates = []
    seen = set()
    for message in messages:
        if message.get("role") == "assistant":
            message_candidates = extract_favor_annotations(message.get("text", ""), project)
        elif message.get("role") == "user":
            user_text = strip_platform_injected_context(message.get("text", ""))
            message_candidates = [
                candidate
                for chunk in split_user_message(user_text)
                for candidate in [score_memory_chunk(chunk, project, threshold)]
                if candidate
            ]
            # A multi-sentence correction often explains one policy in pieces.
            # Keep its first topic-level statement so one message cannot inflate
            # candidate counts or self-promote complementary fragments.
            message_topics = set()
            compact_candidates = []
            for candidate in message_candidates:
                topic_key = (
                    candidate.get("type"),
                    candidate.get("topic"),
                    candidate.get("project"),
                )
                if candidate.get("topic") and topic_key in message_topics:
                    continue
                if candidate.get("topic"):
                    message_topics.add(topic_key)
                compact_candidates.append(candidate)
            message_candidates = compact_candidates
        else:
            continue
        for candidate in message_candidates:
            if not candidate:
                continue
            key = normalize_for_match(candidate["content"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates


def extract_favor_annotations(text, default_project):
    """Extract explicit [FAVOR:...] annotations from assistant replies."""
    candidates = []
    text = strip_markdown_code_blocks(text)
    for raw in re.findall(r"^\s*\[FAVOR:\s*(.*?)\]\s*$", str(text or ""), re.MULTILINE | re.IGNORECASE):
        content, fields = parse_annotation_fields(raw)
        content = normalize_content(
            redact_sensitive(fields.get("content") or fields.get("memory") or content)
        )
        if not content or is_noise(content) or is_question_only(content):
            continue
        memory_type = fields.get("type", "").strip()
        if memory_type not in MEMORY_TYPES:
            memory_type = classify_memory_type(content, topic_signature(content))
        project = normalize_project_slug(fields.get("project", "")) or default_project
        context = normalize_content(
            redact_sensitive(fields.get("context") or fields.get("why") or "")
        )
        topic = topic_signature(content)
        evidence = content if not context else f"{content} | context: {context}"
        assessment = assess_favor(
            {
                "content": content,
                "context": context,
                "type": memory_type,
            }
        )
        if assessment.status == QUALITY_REJECTED:
            continue
        memory_type = assessment.suggested_type or memory_type
        scope = "global" if memory_type == "preference" else "project"
        bound_project = "" if scope == "global" else project
        candidates.append(
            {
                "memory_id": memory_id_for(
                    memory_type,
                    content,
                    project=bound_project,
                    scope=scope,
                ),
                "status": "candidate",
                "type": memory_type,
                "topic": topic,
                "project": bound_project,
                "scope": scope,
                "title": make_title(memory_type, content),
                "content": content,
                "evidence": evidence,
                "polarity": preference_polarity(content),
                "confidence": (
                    EXPLICIT_FAVOR_CONFIDENCE
                    if assessment.status == QUALITY_FORMAL
                    else min(0.74, assessment.score)
                ),
                "seen_count": 0,
                "sources": [],
                "explicit": assessment.status == QUALITY_FORMAL,
                "quality_status": assessment.status,
                "quality_score": assessment.score,
                "quality_reasons": list(assessment.reasons),
            }
        )
    return candidates


def parse_annotation_fields(raw):
    parts = [p.strip() for p in str(raw or "").split("|")]
    leading = parts[0] if parts else ""
    fields = {}
    for part in parts[1:]:
        key, value = split_annotation_field(part)
        if key:
            fields[key] = value
    return leading, fields


def split_annotation_field(part):
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*$", str(part or ""), re.DOTALL)
    if not match:
        return None, ""
    return match.group(1).strip().lower(), match.group(2).strip()


def normalize_project_slug(value):
    return safe_project_slug(value)


def split_user_message(text):
    text = strip_markup(str(text or ""))
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?])\s+|\n+", text)
    chunks = []
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip()
        if len(part) < 8 or len(part) > 240:
            continue
        chunks.append(part)
    return chunks


def strip_markup(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\[[A-Z_]+:.*?\]", " ", text, flags=re.DOTALL)
    return text.strip()


def score_memory_chunk(chunk, project, threshold=0.45):
    lowered = chunk.lower()
    if is_noise(chunk):
        return None
    if is_question_only(chunk):
        return None
    if is_temporary_constraint(chunk):
        return None
    if is_one_off_action_request(chunk):
        return None

    score = 0.0
    if any(pattern in chunk for pattern in PREFERENCE_PATTERNS):
        score += 0.38
    if any(pattern in lowered for pattern in PROJECT_HINT_PATTERNS):
        score += 0.2
    if re.search(r"(以后|默认|优先|不要|保留|自动|重复|待确认)", chunk):
        score += 0.18
    if re.search(r"(^|[，。！？\s])(别|不要|不想|不希望)", chunk):
        score += 0.18
    if re.search(r"(应该|需要|可以|能不能|希望|想法|需求|目的)", chunk):
        score += 0.12
    if re.search(r"(/Users/|~/.codex|~/.claude|~/.zcode|ObsidianBrain|github\\.com)", chunk):
        score += 0.08

    score = min(score, 0.95)
    if score < threshold:
        return None

    topic = topic_signature(chunk)
    memory_type = classify_memory_type(chunk, topic)
    content = normalize_content(redact_sensitive(chunk))
    scope = "global" if memory_type == "preference" else "project"
    bound_project = "" if scope == "global" else project
    return {
        "memory_id": memory_id_for(
            memory_type,
            content,
            project=bound_project,
            scope=scope,
        ),
        "status": "candidate",
        "type": memory_type,
        "topic": topic,
        "project": bound_project,
        "scope": scope,
        "title": make_title(memory_type, content),
        "content": content,
        "evidence": redact_sensitive(chunk),
        "polarity": preference_polarity(content),
        "confidence": round(score, 2),
        "seen_count": 0,
        "sources": [],
    }


def is_noise(chunk):
    stripped = re.sub(r"\s+", "", chunk)
    if len(stripped) < 8:
        return True
    if is_structured_spec_chunk(chunk):
        return True
    if stripped in {"可以", "好的", "继续", "现在怎么样了"}:
        return True
    return any(pattern in chunk and len(chunk) < 20 for pattern in NOISE_PATTERNS)


def is_structured_spec_chunk(chunk):
    """Reject checklist/table/schema fragments that describe a task, not a durable preference."""
    text = str(chunk or "").strip()
    if text.startswith("|") and text.count("|") >= 2:
        return True

    without_marker = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", text).strip()
    has_list_marker = without_marker != text
    first_person = re.search(r"(我的想法|我的需求|我的目的|我希望|我更喜欢|我不想|我不希望)", without_marker)
    if has_list_marker and not first_person:
        return True

    if re.match(r"^[A-Za-z_][A-Za-z0-9_-]{2,40}\s*[:：]", without_marker):
        return True
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*\s+", without_marker) and "：" in without_marker[:60]:
        return True
    return False


def is_question_only(chunk):
    """Reject short clarification questions that are not durable preferences."""
    compact = re.sub(r"\s+", "", chunk)
    if re.search(r"(我想知道|想知道|想问|请问)", compact):
        return True
    if re.search(r"[?？]$", compact) and re.search(
        r"(需要.*吗|是否|是不是|有什么|为什么|怎么|如何|能否)",
        compact,
    ):
        explicit_preference = re.search(
            r"(我希望|我不希望|我更喜欢|请记住|以后请|默认|每次都)",
            compact,
        )
        if not explicit_preference:
            return True
    question_markers = [
        "有什么区别",
        "什么区别",
        "为什么",
        "怎么",
        "如何",
        "是否",
        "是不是",
        "吗",
        "呢",
    ]
    if len(compact) <= 30 and any(marker in compact for marker in question_markers):
        durable_markers = [
            "以后",
            "默认",
            "优先",
            "不要",
            "不想",
            "不希望",
            "我希望",
            "我的需求",
            "我的想法",
        ]
        return not any(marker in compact for marker in durable_markers)
    return False


def is_one_off_action_request(chunk):
    text = str(chunk or "")
    durable = ("以后", "默认", "每次", "总是", "长期")
    if any(term in text for term in durable):
        return False
    return bool(
        re.search(
            r"(帮我|替我|给我).{0,18}(改一下|修改|改成|上传|打开|安装|删除)",
            text,
        )
        or re.search(r"我想把.{1,24}改成", text)
    )


def is_temporary_constraint(chunk):
    temporary = ("本次", "这次", "这一轮", "当前任务", "当前这次", "暂时", "仅这一次")
    durable = ("以后", "默认", "长期", "每次", "总是", "一直")
    return any(term in chunk for term in temporary) and not any(
        term in chunk for term in durable
    )


def preference_polarity(text):
    text = str(text or "")
    if re.search(r"(我不希望|我不想|我不喜欢|不要|别|禁止|不允许|不自动)", text):
        return "negative"
    if re.search(r"(我希望|我想|我更喜欢|默认|优先|保留|自动|应该|需要)", text):
        return "positive"
    return "neutral"


def classify_memory_type(chunk, topic=""):
    if topic == "candidate_memory_policy":
        return "project_rule"
    if re.search(r"(/Users/|~/.codex|~/.claude|~/.zcode|ObsidianBrain|github\\.com)", chunk):
        return "environment"
    if re.search(r"(程序|项目|自动化|记录|Obsidian|obsidian|Codex|codex|Claude|claude|ZCode|zcode|SQLite|sqlite)", chunk):
        return "project_rule"
    return "preference"


def topic_signature(chunk):
    """Group wording variants that express the same preference/rule."""
    rules = [
        (
            "candidate_memory_policy",
            ["不确定", "待确认", "重复", "正式记录", "加到记录", "候选", "转正"],
        ),
        (
            "automation_preference",
            ["自动", "自动化", "自行判断", "不需要手动", "手动触发"],
        ),
        (
            "language_format_preference",
            ["英文标签", "机器标签", "内容中文", "中文内容", "分行"],
        ),
        (
            "obsidian_capture",
            ["obsidian", "Obsidian", "vault", "知识图谱", "记录到"],
        ),
    ]
    for name, terms in rules:
        if any(term in chunk for term in terms):
            return name
    return ""


def normalize_content(chunk):
    chunk = re.sub(r"\s+", " ", chunk).strip()
    chunk = chunk.rstrip("。.!！?？")
    return chunk


def make_title(memory_type, content):
    prefix = {
        "preference": "用户偏好",
        "project_rule": "项目规则",
        "environment": "环境信息",
    }.get(memory_type, "个人记忆")
    short = content
    for lead in ["我的想法是", "我的需求是", "我的目的是", "我希望", "我想"]:
        if short.startswith(lead):
            short = short[len(lead):].strip("，,:： ")
    short = short[:40].strip()
    return f"{prefix}: {short}"


def memory_id_for(memory_type, content, *, project="", scope=""):
    project, scope = candidate_binding(
        {"type": memory_type, "project": project, "scope": scope}
    )
    normalized = normalize_for_match(content)
    digest = hashlib.sha1(
        f"{scope}:{project}:{memory_type}:{normalized}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:10]
    return f"{memory_type}-{digest}"


def candidate_binding(record):
    memory_type = str(record.get("type") or "")
    project = canonical_project(record.get("project"))
    scope = str(
        record.get("scope")
        or (
            "global"
            if memory_type == "preference"
            else "project" if project else "global"
        )
    )
    if scope == "global":
        project = ""
    return project, scope


def normalize_for_match(text):
    text = str(text or "").lower()
    text = re.sub(r"[`*_\\[\\](){}<>#|,，。.!！?？:：;；\"'“”‘’/\\\\-]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def load_candidate_records(candidate_dir):
    records = {}
    if not os.path.isdir(candidate_dir):
        return records
    for filename in os.listdir(candidate_dir):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(candidate_dir, filename)
        record = read_memory_file(path)
        if not record:
            continue
        record["_path"] = path
        records[record.get("memory_id") or filename[:-3]] = record
    return records


def read_memory_file(path):
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
    if not isinstance(fm, dict):
        return {}
    return fm


def find_similar_candidate(candidate, existing, threshold):
    best = None
    best_score = 0.0
    best_threshold = threshold
    for record in existing.values():
        if record.get("type") != candidate.get("type"):
            continue
        if candidate_binding(record) != candidate_binding(candidate):
            continue
        if record.get("memory_id") == candidate.get("memory_id"):
            return record
        record_polarity = record.get("polarity") or preference_polarity(
            record.get("content", "")
        )
        candidate_polarity = candidate.get("polarity") or preference_polarity(
            candidate.get("content", "")
        )
        if (
            record_polarity != "neutral"
            and candidate_polarity != "neutral"
            and record_polarity != candidate_polarity
        ):
            continue
        score = similarity(candidate.get("content", ""), record.get("content", ""))
        if score > best_score:
            best = record
            best_score = score
            same_topic = (
                candidate.get("topic")
                and candidate.get("topic") == record.get("topic")
            )
            best_threshold = max(0.4, threshold * 0.8) if same_topic else threshold
    return best if best and best_score >= best_threshold else None


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


def merge_candidate(candidate, existing, session_id, date_str, now):
    if existing:
        record = dict(existing)
        record["explicit"] = bool(
            record.get("explicit") or candidate.get("explicit")
        )
        is_new_source = not has_source(record, session_id, date_str)
        if is_new_source:
            record["seen_count"] = int(record.get("seen_count", 0)) + 1
            record["confidence"] = round(
                min(0.95, max(float(record.get("confidence", 0)), candidate["confidence"]) + 0.12),
                2,
            )
        else:
            record["seen_count"] = int(record.get("seen_count", 0))
            record["confidence"] = round(
                max(float(record.get("confidence", 0)), candidate["confidence"]),
                2,
            )
        record["last_seen"] = now
        if candidate.get("evidence") and candidate["evidence"] != record.get("evidence"):
            evidence = record.get("evidence_examples") or [record.get("evidence", "")]
            evidence.append(candidate["evidence"])
            record["evidence_examples"] = list(dict.fromkeys(e for e in evidence if e))[-5:]
    else:
        record = dict(candidate)
        record["seen_count"] = 1
        record["first_seen"] = now
        record["last_seen"] = now

    add_stable_source(record, session_id, date_str)
    return sanitize_record(record)


def has_source(record, session_id, date_str):
    return has_stable_source(record, session_id)


def write_candidate_record(candidate_dir, record):
    filename = safe_filename(record.get("title") or record["memory_id"])
    path = os.path.join(candidate_dir, f"{filename}--{record['memory_id']}.md")
    if record.get("_path") and os.path.exists(record["_path"]):
        path = record["_path"]
    record = prepare_candidate_record(record)

    body = [
        f"# {record.get('title', record['memory_id'])}",
        "",
        record.get("content", ""),
        "",
        "## Related",
        "",
        *related_links_for_record(record),
        "",
        "## Evidence",
        "",
        record.get("evidence", ""),
    ]
    if record.get("evidence_examples"):
        body.extend(["", "## Repeated Evidence", ""])
        body.extend(f"- {item}" for item in record["evidence_examples"])

    write_markdown_with_frontmatter(path, record, "\n".join(body).rstrip() + "\n")
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
    record["explicit"] = bool(record.get("explicit"))
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


def append_formal_memory(formal_path, record):
    record = sanitize_record(record)
    existing = ""
    if os.path.exists(formal_path):
        with open(formal_path, "r", encoding="utf-8") as handle:
            existing = handle.read()
        if not formal_memory_update_allowed(existing, record.get("memory_id")):
            return False
        existing = ensure_formal_memory_related_links(existing)
    if not existing.strip():
        existing = (
            "---\n"
            "title: Personal Memory\n"
            "generated_by: memory_judge.py\n"
            f"schema_version: '{RUNTIME_SCHEMA_VERSION}'\n"
            "---\n\n"
            "# Personal Memory\n\n"
            "Promoted memories from repeated or high-confidence conversations.\n"
        )
    before_frontmatter_upgrade = existing
    existing = upgrade_formal_note_frontmatter(
        existing,
        {
            "title": "Personal Memory",
            "generated_by": "memory_judge.py",
        },
    )
    frontmatter_changed = existing != before_frontmatter_upgrade
    existing = ensure_formal_memory_related_links(existing)
    has_existing_record = f"- id: `{record['memory_id']}`" in existing
    lifecycle_metadata = {}
    if has_existing_record:
        lifecycle_metadata = active_formal_lifecycle_metadata(
            existing,
            record["memory_id"],
            "personal",
        )
        if lifecycle_metadata is None:
            return False
    entry = render_formal_memory_entry(record, lifecycle_metadata)

    if has_existing_record:
        updated = replace_formal_memory_entry(
            existing,
            record["memory_id"],
            entry,
        )
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


def render_formal_memory_entry(record, lifecycle_metadata=None):
    memory_type = str(record.get("type") or "preference").replace(
        "environment_fact",
        "environment",
    )
    scope = "global" if memory_type == "preference" else str(record.get("scope") or "project")
    project = "" if scope == "global" else canonical_project(record.get("project"))
    formal = normalize_formal_record(
        {
            "id": record.get("memory_id"),
            "title": record.get("title"),
            "content": record.get("content"),
            "project": project,
            "scope": scope,
            "status": "active",
            "source_refs": [
                f"session:{source_id}"
                for source_id in record.get("source_ids", []) or []
                if source_id
            ],
            **dict(lifecycle_metadata or {}),
        },
        memory_type=memory_type,
        default_project=project,
        source_ref=f"candidate:{record.get('memory_id')}",
    )
    source_refs = ", ".join(f"`{item}`" for item in formal["source_refs"])
    lifecycle_lines = ""
    if formal.get("requires"):
        lifecycle_lines += "- requires: " + ", ".join(
            f"`{item}`" for item in formal["requires"]
        ) + "\n"
    if formal.get("expires_at"):
        lifecycle_lines += f"- expires_at: `{formal['expires_at']}`\n"
    authority_lines = "".join(
        f"{line}\n" for line in render_authority_markdown_lines(formal)
    )
    return (
        f"## {record.get('title', record['memory_id'])}\n\n"
        f"- id: `{formal['id']}`\n"
        f"- revision: `{formal['revision']}`\n"
        f"- type: `{memory_type}`\n"
        "- status: `active`\n"
        f"{lifecycle_lines}"
        f"{authority_lines}"
        f"- scope: `{formal['scope']}`\n"
        f"- project: {project_link(formal.get('project', ''))}\n"
        f"- source_refs: {source_refs}\n"
        f"- confidence: `{record.get('confidence', '')}`\n"
        f"- seen_count: `{record.get('seen_count', '')}`\n"
        f"- memory: {record.get('content', '')}\n"
    )


def replace_formal_memory_entry(existing, memory_id, new_entry):
    headings = list(re.finditer(r"^##\s+.+$", existing, re.MULTILINE))
    for index, heading in enumerate(headings):
        start = heading.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(existing)
        section = existing[start:end]
        if f"- id: `{memory_id}`" not in section:
            continue
        return (
            existing[:start].rstrip()
            + "\n\n"
            + new_entry.rstrip()
            + "\n\n"
            + existing[end:].lstrip("\n")
        )
    return existing


def related_links_for_record(record):
    links = [
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/personal-memory|Personal Memory]]",
    ]
    project = record.get("project", "")
    if project:
        links.extend(
            [
                f"- {project_link(project)}",
                f"- [[01-Projects/{project}/Memory/decisions|{project} decisions]]",
                f"- [[01-Projects/{project}/Memory/pitfalls|{project} pitfalls]]",
            ]
        )
    return links


def project_link(project):
    if not project:
        return "`unknown`"
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"


def sanitize_record(record):
    """Redact persisted strings, including records loaded from older candidates."""
    cleaned = {}
    for key, value in record.items():
        if key == "_path":
            cleaned[key] = value
            continue
        if key == "project":
            cleaned[key] = safe_project_slug(value)
            continue
        if isinstance(value, str):
            cleaned[key] = redact_sensitive(value)
        elif isinstance(value, list):
            cleaned[key] = [
                sanitize_record(item) if isinstance(item, dict) else redact_sensitive(item)
                if isinstance(item, str) else item
                for item in value
            ]
        elif isinstance(value, dict):
            cleaned[key] = sanitize_record(value)
        else:
            cleaned[key] = value
    return cleaned


def ensure_formal_memory_related_links(content):
    if "## Related" in content:
        return content
    links = "\n".join(
        [
            "## Related",
            "",
            "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
            "- [[03-Maps/timeline|Timeline]]",
            "- [[03-Maps/topic-index|Topic Index]]",
            "",
        ]
    )
    return content.replace(
        "Promoted memories from repeated or high-confidence conversations.\n",
        "Promoted memories from repeated or high-confidence conversations.\n\n"
        + links,
        1,
    )


def write_markdown_with_frontmatter(path, frontmatter, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("---\n")
        yaml.dump(frontmatter, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)
        handle.write("---\n\n")
        handle.write(body)
    os.replace(tmp, path)


def safe_filename(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r'[\\/:*?"<>|#^\[\]]+', "", value)
    value = value.strip(" .")
    return value[:72] or "memory-candidate"
