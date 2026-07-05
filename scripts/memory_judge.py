"""Heuristic personal-memory extraction for harvested agent transcripts.

The judge is intentionally conservative. It promotes obvious long-term
preferences directly and keeps weaker signals in a candidate folder until they
repeat across sessions.
"""
import hashlib
import os
import re
from datetime import datetime, timezone, timedelta

import yaml


CST = timezone(timedelta(hours=8))

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
    candidate_dir = os.path.join(vault, settings["candidate_dir"])
    formal_path = os.path.join(vault, settings["formal_path"])
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
            record["confidence"] >= settings["direct_threshold"]
            or record["seen_count"] >= settings["promote_seen_count"]
        )
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
        if message.get("role") != "user":
            continue
        for chunk in split_user_message(message.get("text", "")):
            candidate = score_memory_chunk(chunk, project, threshold)
            if not candidate:
                continue
            key = normalize_for_match(candidate["content"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates


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
    content = normalize_content(chunk)
    return {
        "memory_id": memory_id_for(memory_type, content),
        "status": "candidate",
        "type": memory_type,
        "topic": topic,
        "project": project,
        "title": make_title(memory_type, content),
        "content": content,
        "evidence": chunk,
        "confidence": round(score, 2),
        "seen_count": 0,
        "sources": [],
    }


def is_noise(chunk):
    stripped = re.sub(r"\s+", "", chunk)
    if len(stripped) < 8:
        return True
    if stripped in {"可以", "好的", "继续", "现在怎么样了"}:
        return True
    return any(pattern in chunk and len(chunk) < 20 for pattern in NOISE_PATTERNS)


def is_question_only(chunk):
    """Reject short clarification questions that are not durable preferences."""
    compact = re.sub(r"\s+", "", chunk)
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


def memory_id_for(memory_type, content):
    normalized = normalize_for_match(content)
    digest = hashlib.sha1(f"{memory_type}:{normalized}".encode("utf-8")).hexdigest()[:10]
    return f"{memory_type}-{digest}"


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
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(fm, dict):
        return {}
    return fm


def find_similar_candidate(candidate, existing, threshold):
    best = None
    best_score = 0.0
    for record in existing.values():
        if record.get("type") != candidate.get("type"):
            continue
        if record.get("memory_id") == candidate.get("memory_id"):
            return record
        if record.get("topic") and record.get("topic") == candidate.get("topic"):
            return record
        if record.get("status") == "promoted":
            continue
        score = similarity(candidate.get("content", ""), record.get("content", ""))
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


def merge_candidate(candidate, existing, session_id, date_str, now):
    source = {"session_id": session_id, "date": date_str}
    if existing:
        record = dict(existing)
        record.pop("_path", None)
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

    sources = record.get("sources") or []
    if source not in sources:
        sources.append(source)
    record["sources"] = sources[-10:]
    return record


def has_source(record, session_id, date_str):
    source = {"session_id": session_id, "date": date_str}
    return source in (record.get("sources") or [])


def write_candidate_record(candidate_dir, record):
    filename = safe_filename(record.get("title") or record["memory_id"])
    path = os.path.join(candidate_dir, f"{filename}.md")
    if record.get("_path") and os.path.exists(record["_path"]):
        path = record["_path"]
    record = {k: v for k, v in record.items() if k != "_path"}

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


def append_formal_memory(formal_path, record):
    existing = ""
    if os.path.exists(formal_path):
        with open(formal_path, "r", encoding="utf-8") as handle:
            existing = handle.read()
        existing = ensure_formal_memory_related_links(existing)
    if record["memory_id"] in existing:
        if os.path.exists(formal_path):
            tmp = formal_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(existing)
            os.replace(tmp, formal_path)
        return False

    if not existing.strip():
        existing = (
            "---\n"
            "title: Personal Memory\n"
            "generated_by: memory_judge.py\n"
            "---\n\n"
            "# Personal Memory\n\n"
            "Promoted memories from repeated or high-confidence conversations.\n"
        )
    existing = ensure_formal_memory_related_links(existing)

    entry = (
        f"\n## {record.get('title', record['memory_id'])}\n\n"
        f"- id: `{record['memory_id']}`\n"
        f"- type: `{record.get('type', '')}`\n"
        f"- project: {project_link(record.get('project', ''))}\n"
        f"- confidence: `{record.get('confidence', '')}`\n"
        f"- seen_count: `{record.get('seen_count', '')}`\n"
        f"- memory: {record.get('content', '')}\n"
    )
    tmp = formal_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(existing.rstrip() + "\n" + entry)
    os.replace(tmp, formal_path)
    return True


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
