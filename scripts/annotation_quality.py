#!/usr/bin/env python3
"""Deterministic quality gates for structured memory annotations.

Parsing and semantic acceptance are deliberately separate. Parsers preserve
well-formed tags; this module decides whether a parsed item is durable enough
for formal recall, should wait for confirmation, or is known non-memory noise.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    normalize_iso_date,
    normalize_project_slug,
    redact_sensitive,
    safe_vault_path,
    secure_read_bytes,
    split_frontmatter_text,
)


QUALITY_FORMAL = "formal"
QUALITY_CANDIDATE = "candidate"
QUALITY_REJECTED = "rejected"
QUALITY_STATUSES = frozenset(
    {QUALITY_FORMAL, QUALITY_CANDIDATE, QUALITY_REJECTED}
)

FORMAL_THRESHOLD = 0.65
FORMAL_RECALL_SUPPRESSION_REASONS = frozenset(
    {
        "expected_failure",
        "unresolved",
        "one_off_interaction",
        "temporary_or_one_off",
        "outcome_without_choice",
        "evaluation_outcome",
        "question",
        "missing_content",
        "missing_resolution",
    }
)
ANNOTATION_CANDIDATE_SCHEMA_VERSION = "1.0"
DEFAULT_ANNOTATION_CANDIDATE_DIR = "04-Feedback/_annotation-candidates"
MAX_CANDIDATE_BYTES = 1024 * 1024
MAX_CANDIDATE_SOURCES = 20

DECISION_SIGNALS = re.compile(
    r"(?:采用|选择|使用|保留|改为|替换|统一|固定|优先|暂不|继续让|"
    r"沿用|部署|迁移|接入|启用|禁用|重构|合并|拆分|生成|交付|"
    r"设置|路由|定位为|作为|重画|扩展|补回|覆盖|复用|安装|停止|"
    r"(?:验收|Acceptance).{0,16}(?:只|仅)(?:提升|接受|保留|记录)|"
    r"^(?:将|把|以|由|只|仅|不|先|直接|重新|推荐|要求|项目先|MVP)|"
    r"按照?.{0,12}(?:执行|处理|实现))",
    re.IGNORECASE,
)
DECISION_OUTCOME_ONLY = re.compile(
    r"^(?:已|已经)?(?:完成(?!度)|测试通过|验证通过|检查完成|"
    r"更新完成|修复完成|实现完成)",
    re.IGNORECASE,
)
_EVALUATION_STATUS_PATTERN = (
    r"(?:PASS|BLOCKED|NEEDS\s+REVISION|FAIL(?:ED)?|APPROVED|REJECTED|"
    r"READY\s*:\s*(?:YES|NO)|\b(?:GO|NO[-\s]?GO)\b|"
    r"CRITICAL|IMPORTANT|MAJOR(?:\s+[A-Z][A-Z_-]*)?|"
    r"MINOR(?:\s+[A-Z][A-Z_-]*)?|未通过|通过|阻塞|需修改)"
)
_EVALUATION_ENGLISH_STATUS_PATTERN = (
    r"(?:PASS|BLOCKED|NEEDS\s+REVISION|FAIL(?:ED)?|APPROVED|REJECTED|"
    r"READY\s*:\s*(?:YES|NO)|\b(?:GO|NO[-\s]?GO)\b|"
    r"CRITICAL|IMPORTANT|MAJOR(?:\s+[A-Z][A-Z_-]*)?|"
    r"MINOR(?:\s+[A-Z][A-Z_-]*)?)"
)
_EVALUATION_ACTIVITY_PATTERN = (
    r"(?:审查|复审|评审|验收|专项|结论|判定|review(?:er)?|"
    r"acceptance(?:\s+[A-Z](?:/[A-Z])?)?)"
)
DECISION_EVALUATION_OUTCOME = re.compile(
    rf"(?:"
    rf"{_EVALUATION_ACTIVITY_PATTERN}.{{0,60}}{_EVALUATION_STATUS_PATTERN}"
    rf"|{_EVALUATION_ENGLISH_STATUS_PATTERN}.{{0,36}}"
    rf"{_EVALUATION_ACTIVITY_PATTERN}"
    rf"|通过.{{0,24}}验收"
    rf"|(?:判定(?:为)?|评定为|评为|列为).{{0,36}}"
    rf"(?:{_EVALUATION_STATUS_PATTERN}|(?:已复现的)?(?:主要|严重|关键)?(?:缺陷|问题))"
    rf")",
    re.IGNORECASE,
)
DECISION_EVALUATION_POLICY = re.compile(
    rf"(?:"
    rf"(?:结论|判定).{{0,16}}限定为"
    rf"|{_EVALUATION_STATUS_PATTERN}.{{0,12}}作为.{{0,24}}"
    rf"(?:发布|合并|部署|晋升|准入).{{0,8}}(?:门槛|门禁|条件|依据)"
    rf")",
    re.IGNORECASE,
)
RATIONALE_SIGNALS = re.compile(
    r"(?:因为|由于|避免|确保|需要|能够|可以|便于|兼容|否则|只有|"
    r"同时|符合|依赖|来自|防止|减少|提高|降低|稳定|风险|原因)",
    re.IGNORECASE,
)
TEMPORARY_SIGNALS = re.compile(
    r"(?:这次|本次|本轮|今天|等会|稍后|先别|先不要|当前只是|"
    r"当前任务|本次临时|临时约束|暂时|到时候再|聊完再|还要确认|"
    r"(?:^|[，。；;])临时(?:先|使用|处理|方案))",
    re.IGNORECASE,
)
TEMPORARY_CONTEXT_SIGNALS = re.compile(
    r"(?:本次需求|本轮实现物|本阶段最终产物|当前只是|"
    r"本次临时|临时约束|用户明确要求.{0,12}(?:本次|本轮|这次)|距离上次)",
    re.IGNORECASE,
)
QUESTION_SIGNALS = re.compile(r"(?:[?？]\s*$|^(?:是否|能否|怎么|如何|为什么))")

ERROR_FAILURE_SIGNALS = re.compile(
    r"(?:报错|失败|缺少|没有|不存在|不可用|未安装|不支持|拒绝|"
    r"超时|冲突|损坏|乱码|异常|错误|无权限|未运行|找不到|"
    r"not found|failed|failure|error|timeout|unsupported|denied)",
    re.IGNORECASE,
)
ERROR_ACTION_SIGNALS = re.compile(
    r"(?:改用|修复|更正|重新|重建|补齐|补充|安装|清除|移除|恢复|"
    r"替换|关闭|重启|调整|定位|回滚|转为|绕过|加引号|改成|"
    r"使用.{0,20}(?:替代|完成|运行|提取|验证))",
    re.IGNORECASE,
)
ERROR_VERIFICATION_SIGNALS = re.compile(
    r"(?:验证通过|测试通过|检查通过|核对完成|完成核对|完成校验|"
    r"完成.{1,24}(?:校验|核对|验证|检查)|"
    r"成功|正常|恢复|解决|可用|输出正确|继续完成|最终完成|"
    r"passed|verified|succeeded|works)",
    re.IGNORECASE,
)
ERROR_UNRESOLVED_SIGNALS = re.compile(
    r"(?:尚未|仍未|未解决|未修复|待处理|待确认|无法继续|"
    r"还没有解决|需要后续|仍然失败)",
    re.IGNORECASE,
)
EXPECTED_FAILURE_SIGNALS = re.compile(
    r"(?:TDD\s*RED|预期失败|故意失败|先写失败测试|expected\s+(?:red|failure))",
    re.IGNORECASE,
)
ONE_OFF_ERROR_SIGNALS = re.compile(
    r"(?:(?<!错)误触|点错|界面状态未刷新|未按预期选中|首次点击|鼠标点击)",
    re.IGNORECASE,
)

FAVOR_DURABLE_SIGNALS = re.compile(
    r"(?:以后|默认|优先|保留|统一|每次|始终|长期|自动|尽量|"
    r"必须|应当|不要|不希望|希望|更喜欢|使用|位于|目录是|路径是|"
    r"(?:时|前).{0,16}(?:要|需|应|必须|不要|先|用|使用|检查|查看|记录|输出)|"
    r"要(?:具体|先|保持|使用|检查|查看|记录|输出|避免|遵守))",
    re.IGNORECASE,
)
ENVIRONMENT_SIGNALS = re.compile(
    r"(?:macOS|MacBook|Windows|Linux|电脑|系统|运行环境|配置目录|"
    r"安装目录|路径|~\/|\/Users\/|\.codex|\.claude)",
    re.IGNORECASE,
)
PROJECT_RULE_SIGNALS = re.compile(
    r"(?:项目中|本项目|代码审查|工作流|流程|提交前|生成的|"
    r"session|Skill\s*调用|仓库|源码)",
    re.IGNORECASE,
)

FAILURE_MODES = (
    (
        "type_conversion",
        re.compile(
            r"(?:(?:stderr|stdout|output|返回值|输出).{0,32}(?:bytes?|字节)"
            r"|(?:bytes?|字节).{0,32}(?:str|string|文本))",
            re.IGNORECASE,
        ),
    ),
    (
        "api_unsupported",
        re.compile(
            r"(?:(?:不支持|unsupported).{0,40}(?:dir_fd|参数|argument|option)"
            r"|(?:dir_fd|argument|option).{0,40}(?:不支持|unsupported))",
            re.IGNORECASE,
        ),
    ),
    (
        "missing",
        re.compile(
            r"(?:缺少|没有|不存在|不可用|未安装|找不到|不在.{0,12}PATH|"
            r"not found|no module named|command not found)",
            re.IGNORECASE,
        ),
    ),
    (
        "permission",
        re.compile(
            r"(?:无权限|permission denied|拒绝访问|"
            r"(?:文件系统|文件|目录|路径|卷|挂载).{0,12}(?:为|处于|是)?"
            r"只读(?!审查|读取|提取|核对|检查|访问|验证)|"
            r"只读.{0,12}(?:导致|无法|错误|报错|拒绝))",
            re.IGNORECASE,
        ),
    ),
    (
        "timeout",
        re.compile(r"(?:超时|timeout|timed out)", re.IGNORECASE),
    ),
    (
        "format",
        re.compile(r"(?:乱码|解析失败|格式错误|损坏|corrupt|parse error)", re.IGNORECASE),
    ),
    (
        "auth",
        re.compile(r"(?:认证|授权|无权限|API Key|OAuth|auth)", re.IGNORECASE),
    ),
    (
        "conflict",
        re.compile(r"(?:冲突|不兼容|conflict|incompatible)", re.IGNORECASE),
    ),
)

CONCEPT_TERMS = (
    "pdf",
    "文本",
    "渲染",
    "字体",
    "路径",
    "网络",
    "认证",
    "授权",
    "数据库",
    "测试",
    "前端",
    "配置",
    "依赖",
    "安装",
    "容器",
    "git",
    "github",
    "obsidian",
    "codex",
)

GENERIC_LATIN_TOKENS = frozenset(
    {
        "error",
        "failed",
        "failure",
        "file",
        "path",
        "system",
        "python",
        "command",
        "output",
        "input",
        "test",
        "tests",
        "text",
        "other",
        "shell-cli",
        "path-filesystem",
    }
)


@dataclass(frozen=True)
class QualityAssessment:
    status: str
    score: float
    reasons: tuple[str, ...] = ()
    suggested_type: str = ""

    def __post_init__(self):
        if self.status not in QUALITY_STATUSES:
            raise ValueError(f"invalid annotation quality status: {self.status}")


def partition_annotations(decisions, errors):
    """Separate parsed annotations into formal, candidate, and rejected paths."""
    result = {
        "decisions": [],
        "errors": [],
        "candidates": [],
        "rejected": [],
    }
    for annotation_type, items, assessor, formal_key in (
        ("decision", decisions, assess_decision, "decisions"),
        ("error", errors, assess_error, "errors"),
    ):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            assessment = assessor(item)
            if assessment.status == QUALITY_FORMAL:
                result[formal_key].append(item)
                continue
            candidate = _candidate_payload(annotation_type, item, assessment)
            destination = (
                "candidates"
                if assessment.status == QUALITY_CANDIDATE
                else "rejected"
            )
            result[destination].append(candidate)
    return result


def process_annotation_candidates(cfg, candidates, project, session_id, date_str):
    """Persist uncertain explicit tags without making them runtime memory."""
    settings = annotation_quality_settings(cfg)
    result = {"candidates": 0, "updated": 0, "items": []}
    if not settings["enabled"] or not candidates:
        return result
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    if not vault or not os.path.isdir(vault):
        raise ValueError("annotation candidates require a configured Vault")
    candidate_dir = safe_vault_path(vault, settings["candidate_dir"])
    ensure_directory_tree(candidate_dir, vault)
    normalized_project = normalize_project_slug(project)
    normalized_session = _source_session(session_id)
    normalized_date = normalize_iso_date(date_str, datetime.now().date().isoformat())

    for candidate in candidates:
        record = _candidate_record(
            candidate,
            normalized_project,
            normalized_session,
            normalized_date,
        )
        path = safe_vault_path(candidate_dir, f"{record['annotation_id']}.md")
        existing = _read_candidate(path, vault)
        is_new_source = normalized_session not in (existing.get("source_sessions") or [])
        if existing:
            sources = list(existing.get("source_sessions") or [])
            if is_new_source:
                sources.append(normalized_session)
            record["source_sessions"] = list(dict.fromkeys(sources))[-MAX_CANDIDATE_SOURCES:]
            record["seen_count"] = len(record["source_sessions"])
            record["first_seen"] = existing.get("first_seen") or record["first_seen"]
            record["quality_score"] = max(
                float(existing.get("quality_score") or 0),
                float(record.get("quality_score") or 0),
            )
            record["quality_reasons"] = sorted(
                set(existing.get("quality_reasons") or [])
                | set(record.get("quality_reasons") or [])
            )
        record["revision"] = annotation_candidate_revision(record)
        existing_revision = str(existing.get("revision") or "")
        if existing_revision == record["revision"]:
            continue
        durable_atomic_write(path, _render_candidate(record), root=vault)
        action = "candidate" if not existing else "updated"
        result["candidates" if not existing else "updated"] += 1
        result["items"].append(
            {
                "action": action,
                "annotation_type": record["annotation_type"],
                "title": record["title"],
                "quality_score": record["quality_score"],
                "quality_reasons": record["quality_reasons"],
                "seen_count": record["seen_count"],
                "path": os.path.relpath(path, vault).replace(os.sep, "/"),
            }
        )
    return result


def annotation_quality_settings(cfg):
    raw = cfg.get("annotation_quality") or {}
    if not isinstance(raw, dict):
        raise TypeError("config annotation_quality must be a mapping")
    return {
        "enabled": raw.get("enabled", True),
        "candidate_dir": raw.get(
            "candidate_dir", DEFAULT_ANNOTATION_CANDIDATE_DIR
        ),
    }


def annotation_candidate_roots(cfg):
    vault = cfg.get("vault_path")
    raw = annotation_quality_settings(cfg)["candidate_dir"]
    candidate = safe_vault_path(vault, raw)
    relative = os.path.relpath(candidate, vault).replace(os.sep, "/")
    return tuple(dict.fromkeys((relative, DEFAULT_ANNOTATION_CANDIDATE_DIR)))


def is_annotation_candidate_path(path, candidate_roots=None):
    canonical = "/" + str(path or "").replace("\\", "/").strip("/").casefold()
    for root in candidate_roots or (DEFAULT_ANNOTATION_CANDIDATE_DIR,):
        normalized = "/" + str(root or "").replace("\\", "/").strip("/").casefold()
        if canonical == normalized or canonical.startswith(normalized + "/"):
            return True
    return False


def annotation_candidate_revision(record):
    visible = {
        key: value
        for key, value in record.items()
        if key not in {"revision", "last_seen"}
    }
    return hashlib.sha256(
        json.dumps(visible, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def assess_decision(item):
    title = _one_line((item or {}).get("text") or (item or {}).get("title"))
    context = _one_line((item or {}).get("context") or (item or {}).get("summary"))
    reasons = []
    if not title:
        return QualityAssessment(QUALITY_REJECTED, 0.0, ("missing_content",))
    if QUESTION_SIGNALS.search(title):
        return QualityAssessment(QUALITY_REJECTED, 0.05, ("question",))

    score = 0.25
    if len(title) >= 8:
        score += 0.1
    if len(context) >= 8:
        score += 0.2
    else:
        reasons.append("missing_rationale")
    if DECISION_SIGNALS.search(title):
        score += 0.25
    else:
        reasons.append("decision_signal_unclear")
    if RATIONALE_SIGNALS.search(context) or len(context) >= 20:
        score += 0.15

    if DECISION_OUTCOME_ONLY.search(title):
        score -= 0.45
        _append_reason(reasons, "outcome_without_choice")
    if (
        DECISION_EVALUATION_OUTCOME.search(title)
        and not DECISION_EVALUATION_POLICY.search(title)
    ):
        score -= 0.35
        _append_reason(reasons, "evaluation_outcome")
    if TEMPORARY_SIGNALS.search(title) or TEMPORARY_CONTEXT_SIGNALS.search(context):
        score -= 0.35
        _append_reason(reasons, "temporary_or_one_off")

    blockers = {
        "missing_rationale",
        "decision_signal_unclear",
        "outcome_without_choice",
        "evaluation_outcome",
        "temporary_or_one_off",
    }
    status = (
        QUALITY_FORMAL
        if score >= FORMAL_THRESHOLD and not blockers.intersection(reasons)
        else QUALITY_CANDIDATE
    )
    return QualityAssessment(status, _bounded(score), tuple(reasons))


def assess_error(item):
    error_type = _one_line(
        (item or {}).get("error_type")
        or (item or {}).get("type")
        or (item or {}).get("title")
    )
    resolution = _one_line(
        (item or {}).get("resolution") or (item or {}).get("summary")
    )
    if not resolution:
        return QualityAssessment(QUALITY_REJECTED, 0.0, ("missing_resolution",))
    if EXPECTED_FAILURE_SIGNALS.search(resolution):
        return QualityAssessment(QUALITY_REJECTED, 0.0, ("expected_failure",))

    reasons = []
    score = 0.2
    if re.fullmatch(r"[A-Za-z0-9_.\/-]{2,96}", error_type):
        score += 0.1
    else:
        reasons.append("invalid_error_type")
    if len(resolution) >= 12:
        score += 0.1
    if ERROR_FAILURE_SIGNALS.search(resolution):
        score += 0.15
    else:
        reasons.append("missing_failure_signal")
    if ERROR_ACTION_SIGNALS.search(resolution):
        score += 0.25
    else:
        reasons.append("missing_resolution_action")
    verified = bool(ERROR_VERIFICATION_SIGNALS.search(resolution))
    if verified:
        score += 0.2
    else:
        reasons.append("missing_verification")
    if _distinctive_anchors(resolution):
        score += 0.1

    if ERROR_UNRESOLVED_SIGNALS.search(resolution) and not verified:
        score -= 0.35
        reasons.append("unresolved")
    if ONE_OFF_ERROR_SIGNALS.search(resolution):
        score -= 0.2
        reasons.append("one_off_interaction")

    blockers = {
        "invalid_error_type",
        "missing_failure_signal",
        "missing_resolution_action",
        "missing_verification",
        "unresolved",
        "one_off_interaction",
    }
    status = (
        QUALITY_FORMAL
        if score >= FORMAL_THRESHOLD and not blockers.intersection(reasons)
        else QUALITY_CANDIDATE
    )
    return QualityAssessment(status, _bounded(score), tuple(dict.fromkeys(reasons)))


def assess_favor(item):
    content = _one_line(
        (item or {}).get("content")
        or (item or {}).get("memory")
        or (item or {}).get("summary")
    )
    context = _one_line((item or {}).get("context") or (item or {}).get("evidence"))
    requested_type = _one_line((item or {}).get("type"))
    suggested_type = infer_favor_type(content, requested_type)
    if not content:
        return QualityAssessment(
            QUALITY_REJECTED,
            0.0,
            ("missing_content",),
            suggested_type,
        )
    if QUESTION_SIGNALS.search(content):
        return QualityAssessment(
            QUALITY_REJECTED,
            0.05,
            ("question",),
            suggested_type,
        )

    reasons = []
    score = 0.25
    if len(content) >= 8:
        score += 0.1
    if len(context) >= 8:
        score += 0.15
    else:
        reasons.append("missing_context")
    if FAVOR_DURABLE_SIGNALS.search(content) or suggested_type == "environment":
        score += 0.25
    else:
        reasons.append("durability_unclear")
    if suggested_type in {"preference", "project_rule", "environment"}:
        score += 0.1
    if RATIONALE_SIGNALS.search(context) or len(context) >= 18:
        score += 0.1

    if TEMPORARY_SIGNALS.search(content) or TEMPORARY_CONTEXT_SIGNALS.search(context):
        score -= 0.4
        reasons.append("temporary_or_one_off")

    if "temporary_or_one_off" in reasons:
        status = QUALITY_REJECTED
    else:
        blockers = {"durability_unclear"}
        status = (
            QUALITY_FORMAL
            if score >= FORMAL_THRESHOLD and not blockers.intersection(reasons)
            else QUALITY_CANDIDATE
        )
    return QualityAssessment(
        status,
        _bounded(score),
        tuple(dict.fromkeys(reasons)),
        suggested_type,
    )


def infer_favor_type(content, requested_type=""):
    requested = str(requested_type or "").strip().lower()
    text = _one_line(content)
    if requested == "environment":
        return "environment"
    if ENVIRONMENT_SIGNALS.search(text) and re.search(
        r"(?:使用|位于|目录|路径|安装|运行|系统|电脑)", text, re.IGNORECASE
    ):
        return "environment"
    if requested == "project_rule" or PROJECT_RULE_SIGNALS.search(text):
        return "project_rule"
    return "preference"


def collapse_runtime_duplicates(records):
    """Return a derived runtime view with conservative near-duplicates folded.

    Formal source records are never mutated. A record referenced through
    ``requires`` is kept independently so dependency semantics remain intact.
    """
    source = [copy.deepcopy(item) for item in records or [] if isinstance(item, dict)]
    required_ids = {
        str(required)
        for item in source
        for required in (item.get("requires") or [])
        if str(required)
    }
    grouped = {}
    for item in source:
        item_type = str(item.get("type") or "")
        key = (
            item_type,
            str(item.get("project") or ""),
            str(item.get("scope") or ""),
            "" if item_type == "error" else str(item.get("title") or "").casefold(),
        )
        grouped.setdefault(key, []).append(item)

    collapsed = []
    duplicate_groups = []
    for key in sorted(grouped):
        clusters = []
        for item in sorted(grouped[key], key=lambda row: str(row.get("id") or "")):
            if item.get("id") in required_ids:
                clusters.append([item])
                continue
            destination = None
            for cluster in clusters:
                if any(member.get("id") in required_ids for member in cluster):
                    continue
                if _records_are_duplicates(cluster[0], item):
                    destination = cluster
                    break
            if destination is None:
                clusters.append([item])
            else:
                destination.append(item)

        for cluster in clusters:
            if len(cluster) == 1:
                collapsed.append(cluster[0])
                continue
            representative = max(cluster, key=_representative_rank)
            merged = copy.deepcopy(representative)
            duplicate_ids = sorted(
                str(item.get("id"))
                for item in cluster
                if item.get("id") != representative.get("id")
            )
            merged["duplicate_ids"] = duplicate_ids
            aliases = set(merged.get("aliases") or [])
            aliases.update(duplicate_ids)
            merged["aliases"] = sorted(aliases)
            refs = set(merged.get("source_refs") or [])
            for item in cluster:
                refs.update(item.get("source_refs") or [])
            merged["source_refs"] = sorted(refs)
            collapsed.append(merged)
            duplicate_groups.append(
                {
                    "representative_id": str(representative.get("id") or ""),
                    "member_ids": sorted(
                        str(item.get("id") or "") for item in cluster
                    ),
                    "reason": _duplicate_reason(cluster[0], cluster[1]),
                }
            )

    collapsed.sort(key=lambda item: str(item.get("id") or ""))
    duplicate_groups.sort(
        key=lambda item: (item["representative_id"], item["member_ids"])
    )
    return collapsed, duplicate_groups


def filter_runtime_quality(records):
    """Suppress only high-confidence non-memory while preserving formal sources."""
    eligible = []
    suppressed = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        memory_type = str(record.get("type") or "")
        if memory_type == "decision":
            assessment = assess_decision(record)
        elif memory_type == "error":
            assessment = assess_error(record)
        elif memory_type in {"preference", "project_rule", "environment"}:
            assessment = assess_favor(
                {
                    "content": record.get("summary"),
                    "context": record.get("title"),
                    "type": memory_type,
                }
            )
        else:
            eligible.append(copy.deepcopy(record))
            continue
        reasons = sorted(
            set(assessment.reasons) & FORMAL_RECALL_SUPPRESSION_REASONS
        )
        if reasons:
            suppressed[str(record.get("id") or "")] = reasons
        else:
            eligible.append(copy.deepcopy(record))
    return eligible, suppressed


def _records_are_duplicates(left, right):
    if any(
        str(left.get(field) or "") != str(right.get(field) or "")
        for field in ("type", "project", "scope")
    ):
        return False
    if (
        left.get("type") != "error"
        and str(left.get("title") or "") != str(right.get("title") or "")
    ):
        return False
    operational_fields = {
        "skill": ("name", "when", "avoid"),
        "workflow": ("name", "trigger", "behavior", "avoid"),
    }.get(left.get("type"), ())
    if any(
        _one_line(left.get(field)) != _one_line(right.get(field))
        for field in operational_fields
    ):
        return False
    left_text = _one_line(left.get("summary"))
    right_text = _one_line(right.get("summary"))
    if not left_text or not right_text or left_text == right_text:
        return bool(left_text)

    left_tokens = _semantic_tokens(left_text)
    right_tokens = _semantic_tokens(right_text)
    similarity = _jaccard(left_tokens, right_tokens)
    if left.get("type") != "error":
        return similarity >= 0.82

    left_modes = _failure_modes(left_text)
    right_modes = _failure_modes(right_text)
    if not left_modes or left_modes != right_modes:
        return False
    left_anchors = _failure_anchors(left_text)
    right_anchors = _failure_anchors(right_text)
    if left_anchors != right_anchors:
        return False
    shared_anchors = left_anchors & right_anchors
    shared_concepts = _concepts(left_text) & _concepts(right_text)
    return bool(shared_anchors and shared_concepts) or similarity >= 0.72


def _candidate_payload(annotation_type, item, assessment):
    if annotation_type == "decision":
        title = _one_line(item.get("text") or item.get("title"))
        summary = _one_line(item.get("context") or item.get("summary"))
    else:
        title = _one_line(item.get("type") or item.get("error_type"))
        summary = _one_line(item.get("resolution") or item.get("summary"))
    return {
        "annotation_type": annotation_type,
        "title": redact_sensitive(title),
        "summary": redact_sensitive(summary),
        "quality_status": assessment.status,
        "quality_score": assessment.score,
        "quality_reasons": list(assessment.reasons),
    }


def _candidate_record(candidate, project, session_id, date_str):
    annotation_type = str(candidate.get("annotation_type") or "").strip()
    if annotation_type not in {"decision", "error"}:
        raise ValueError("invalid annotation candidate type")
    title = _one_line(redact_sensitive(candidate.get("title")))
    summary = _one_line(redact_sensitive(candidate.get("summary")))
    if not title or not summary:
        raise ValueError("annotation candidate requires title and summary")
    identity = hashlib.sha256(
        "\x1f".join(
            [annotation_type, project, title.casefold(), summary.casefold()]
        ).encode("utf-8")
    ).hexdigest()[:20]
    now = datetime.now(timezone.utc).isoformat()
    return {
        "annotation_id": f"annotation-candidate-{identity}",
        "schema_version": ANNOTATION_CANDIDATE_SCHEMA_VERSION,
        "type": "annotation-candidate",
        "status": "candidate",
        "annotation_type": annotation_type,
        "project": project,
        "title": title,
        "summary": summary,
        "quality_status": QUALITY_CANDIDATE,
        "quality_score": _bounded(candidate.get("quality_score") or 0),
        "quality_reasons": sorted(
            {
                _one_line(reason)
                for reason in candidate.get("quality_reasons") or []
                if _one_line(reason)
            }
        ),
        "seen_count": 1,
        "source_sessions": [session_id],
        "first_seen": now,
        "last_seen": now,
        "last_seen_date": date_str,
        "revision": "",
        "generated_by": "annotation_quality.py",
    }


def _read_candidate(path, vault):
    try:
        data = secure_read_bytes(path, MAX_CANDIDATE_BYTES, root=vault)
    except FileNotFoundError:
        return {}
    if len(data) > MAX_CANDIDATE_BYTES:
        raise ValueError("annotation candidate exceeds size limit")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("annotation candidate is not UTF-8") from exc
    frontmatter_text, _body = split_frontmatter_text(content)
    if frontmatter_text is None:
        raise ValueError("annotation candidate frontmatter is missing")
    try:
        record = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError("annotation candidate frontmatter is invalid") from exc
    if not isinstance(record, dict):
        raise ValueError("annotation candidate frontmatter must be a mapping")
    if (
        record.get("schema_version") != ANNOTATION_CANDIDATE_SCHEMA_VERSION
        or record.get("type") != "annotation-candidate"
        or record.get("status") != "candidate"
    ):
        raise ValueError("annotation candidate schema is invalid")
    expected_revision = annotation_candidate_revision(record)
    if record.get("revision") != expected_revision:
        raise ValueError("annotation candidate revision is invalid")
    return record


def _render_candidate(record):
    reasons = ", ".join(f"`{item}`" for item in record["quality_reasons"]) or "-"
    project_link = (
        f"[[01-Projects/{record['project']}/Memory/decisions|{record['project']}]]"
        if record.get("project")
        else "`global`"
    )
    body = "\n".join(
        [
            f"# 待确认 {record['annotation_type']}: {record['title']}",
            "",
            f"- 质量原因: {reasons}",
            f"- 出现次数: `{record['seen_count']}`",
            f"- 项目: {project_link}",
            "",
            "## 内容",
            "",
            record["summary"],
            "",
            "该条目尚未进入正式记忆或运行时召回。",
            "",
        ]
    )
    return (
        "---\n"
        + yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )


def _source_session(value):
    session = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    if not session or len(session) > 200:
        return "session-" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    return session


def _duplicate_reason(left, right):
    if left.get("type") == "error":
        anchors = sorted(
            _failure_anchors(left.get("summary"))
            & _failure_anchors(right.get("summary"))
        )
        if anchors:
            return "same_failure_anchor:" + ",".join(anchors[:3])
    return "high_semantic_overlap"


def _representative_rank(item):
    if item.get("type") == "decision":
        quality = assess_decision(item)
    elif item.get("type") == "error":
        quality = assess_error(item)
    else:
        quality = assess_favor(
            {
                "content": item.get("summary"),
                "context": item.get("title"),
                "type": item.get("type"),
            }
        )
    summary = _one_line(item.get("summary"))
    useful_length = min(len(summary), 240)
    return (
        quality.status == QUALITY_FORMAL,
        quality.score,
        len(item.get("source_refs") or []),
        useful_length,
        str(item.get("date") or ""),
        str(item.get("id") or ""),
    )


def _failure_mode(text):
    for name, pattern in FAILURE_MODES:
        if pattern.search(str(text or "")):
            return name
    return ""


def _failure_modes(text):
    return {
        name
        for name, pattern in FAILURE_MODES
        if pattern.search(str(text or ""))
    }


def _failure_anchors(text):
    """Extract the failed object near a failure marker, not workaround tools."""
    value = str(text or "")
    anchors = set()
    missing_after = re.finditer(
        r"(?:缺少|没有|未安装|找不到)\s*(?:可用的\s*)?([^，；。且]{1,80})",
        value,
        re.IGNORECASE,
    )
    for match in missing_after:
        tokens = _anchor_tokens(match.group(1))
        if tokens:
            anchors.add(tokens[0])
    environment_absence = re.finditer(
        r"(?:环境|系统|运行时)(?:中)?\s*无\s*([^，；。且]{1,80})",
        value,
        re.IGNORECASE,
    )
    for match in environment_absence:
        tokens = _anchor_tokens(match.group(1))
        if tokens:
            anchors.add(tokens[0])
    subject_markers = re.finditer(
        r"(?:路径\s*)?(?:不存在|不可用|不在.{0,12}PATH|超时|timeout)",
        value,
        re.IGNORECASE,
    )
    for match in subject_markers:
        tokens = _anchor_tokens(value[max(0, match.start() - 100) : match.start()])
        if tokens:
            anchors.add(tokens[-1])
    if _failure_mode(value) == "timeout" and not anchors:
        prefix = re.split(r"(?:超时|timeout|timed out)", value, maxsplit=1, flags=re.IGNORECASE)[0]
        tokens = _anchor_tokens(prefix[-100:])
        if tokens:
            anchors.add(tokens[-1])
    return anchors


def _anchor_tokens(text):
    aliases = {
        "pypdf2": "pypdf",
        "python3": "python",
    }
    ignored = GENERIC_LATIN_TOKENS | {
        "cli",
        "runtime",
        "bundled",
        "default",
        "current",
        "actual",
        "skill",
        "skills",
        "api",
    }
    result = []
    for token in re.findall(
        r"(?<![\w.-])[A-Za-z][A-Za-z0-9_.+-]{1,}(?![\w.-])",
        str(text or ""),
    ):
        normalized = aliases.get(token.casefold(), token.casefold())
        if normalized not in ignored:
            result.append(normalized)
    return result


def _distinctive_anchors(text):
    anchors = {
        token.casefold()
        for token in re.findall(
            r"(?<![\w.-])[A-Za-z][A-Za-z0-9_.+-]{2,}(?![\w.-])",
            str(text or ""),
        )
        if token.casefold() not in GENERIC_LATIN_TOKENS
    }
    return anchors


def _concepts(text):
    lowered = str(text or "").casefold()
    return {term.casefold() for term in CONCEPT_TERMS if term.casefold() in lowered}


def _semantic_tokens(text):
    normalized = str(text or "").casefold()
    tokens = set(_distinctive_anchors(normalized))
    tokens.update(_concepts(normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def _jaccard(left, right):
    union = set(left) | set(right)
    if not union:
        return 0.0
    return len(set(left) & set(right)) / len(union)


def _one_line(value):
    return " ".join(str(value or "").split())


def _bounded(value):
    return round(max(0.0, min(1.0, float(value))), 2)


def _append_reason(reasons, reason):
    if reason not in reasons:
        reasons.append(reason)
