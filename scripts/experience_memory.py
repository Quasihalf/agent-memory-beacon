"""Derive bounded task-experience bundles from formal memory records."""
import hashlib
import re
from collections import defaultdict

from memory_schema import (
    RUNTIME_MEMORY_TYPES,
    canonical_project,
    is_valid_memory_id,
    is_valid_runtime_record,
)


EXPERIENCE_INTENT_PATTERN = re.compile(
    r"(?:类似经验|相似经验|以前怎么(?:做|处理|解决|完成)|"
    r"之前怎么(?:做|处理|解决|完成)|过去怎么(?:做|处理|解决|完成)|"
    r"完整过程|完整经历|从头到尾|复盘(?:一下)?|"
    r"\b(?:similar experience|previous experience|how did (?:we|you)|"
    r"full process|complete process|end[- ]to[- ]end|retrospective)\b)",
    re.IGNORECASE,
)
EXPERIENCE_ANCHOR_FILLER_PATTERN = re.compile(
    r"(?:以前|之前|过去|当时|怎么|如何|做|处理|解决|完成|"
    r"完整过程|完整经历|从头到尾|复盘|一下|这个|那个|相关|的|"
    r"\b(?:similar|previous|experience|how|did|we|you|full|complete|"
    r"process|end|to|retrospective|the|a|an)\b)",
    re.IGNORECASE,
)
_NON_FORMAL_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:_[^/]*(?:candidate|proposal)[^/]*)(?:/|$)",
    re.IGNORECASE,
)
_COMPANION_TYPE_RANK = {
    "error": 8,
    "workflow": 7,
    "decision": 6,
    "insight": 5,
    "skill": 4,
    "project_rule": 3,
    "preference": 2,
    "environment": 1,
}
_GENERIC_AFFINITY_FEATURES = frozenset(
    {
        "error",
        "other",
        "decision",
        "workflow",
        "project",
        "memory",
        "result",
        "using",
        "使用",
        "采用",
        "当前",
        "结果",
        "方案",
        "问题",
        "错误",
        "修复",
        "验证",
        "模型",
        "完成",
        "处理",
        "进行",
        "内容",
        "需要",
        "通过",
        "可以",
        "导致",
        "实现",
        "相关",
        "同时",
    }
)


def build_experience_bundles(units):
    """Group exact formal revisions that share one project session source."""
    groups = defaultdict(dict)
    for unit in units or []:
        if not _eligible_member(unit):
            continue
        project = str(unit.get("project") or "").strip()
        for session_ref in _session_refs(unit):
            groups[(project, session_ref)][unit["id"]] = unit

    bundles = []
    for (project, session_ref), members_by_id in sorted(groups.items()):
        members = list(members_by_id.values())
        if len(members) < 2 or len({item.get("type") for item in members}) < 2:
            continue
        member_refs = [_member_ref(item, session_ref) for item in members]
        member_refs.sort(
            key=lambda item: (
                item["type"],
                item["id"],
                item["revision"],
            )
        )
        bundles.append(
            {
                "id": _bundle_id(project, session_ref),
                "project": project,
                "session_ref": session_ref,
                "date": max(str(item.get("date") or "") for item in members),
                "memory_types": sorted({item["type"] for item in member_refs}),
                "members": member_refs,
            }
        )
    bundles.sort(
        key=lambda item: (
            item["project"],
            item["date"],
            item["session_ref"],
            item["id"],
        ),
        reverse=True,
    )
    return bundles


def has_experience_intent(query, *, content_query="", inventory_query=False):
    """Require an explicit experience request plus a concrete content anchor."""
    if inventory_query or not EXPERIENCE_INTENT_PATTERN.search(str(query or "")):
        return False
    anchor = EXPERIENCE_INTENT_PATTERN.sub(" ", str(content_query or query or ""))
    anchor = EXPERIENCE_ANCHOR_FILLER_PATTERN.sub(" ", anchor)
    anchor = " ".join(anchor.split())
    return bool(re.search(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", anchor, re.IGNORECASE))


def select_experience_companions(
    index,
    seed_ids,
    eligible_units,
    *,
    max_companions=2,
    max_bundles=1,
):
    """Return current formal units related through at most one exact bundle."""
    seeds = {str(item) for item in seed_ids or [] if str(item)}
    units_by_id = {
        item.get("id"): item
        for item in eligible_units or []
        if isinstance(item, dict) and item.get("id")
    }
    if not seeds or not units_by_id:
        return []

    candidates = []
    for bundle in index.get("experience_bundles") or []:
        if not isinstance(bundle, dict):
            continue
        current_members = []
        for member in bundle.get("members") or []:
            if not isinstance(member, dict):
                continue
            unit = units_by_id.get(member.get("id"))
            if (
                unit
                and unit.get("revision") == member.get("revision")
                and unit.get("project") == bundle.get("project")
            ):
                current_members.append(unit)
        seed_hits = seeds & {item["id"] for item in current_members}
        if not seed_hits:
            continue
        seed_units = [item for item in current_members if item["id"] in seed_hits]
        companions = []
        for unit in current_members:
            if unit["id"] in seeds:
                continue
            affinity = _companion_affinity(unit, seed_units)
            if affinity > 0:
                companions.append((unit, affinity))
        if not companions:
            continue
        candidates.append((bundle, seed_hits, companions))

    candidates.sort(
        key=lambda item: (
            max(score for _unit, score in item[2]),
            sum(score for _unit, score in item[2]),
            len(item[1]),
            str(item[0].get("date") or ""),
            str(item[0].get("id") or ""),
        ),
        reverse=True,
    )
    selected = []
    for bundle, _, companions in candidates[: max(0, int(max_bundles or 0))]:
        companions.sort(
            key=lambda item: (
                item[1],
                _COMPANION_TYPE_RANK.get(item[0].get("type"), 0),
                str(item[0].get("date") or ""),
                str(item[0].get("id") or ""),
            ),
            reverse=True,
        )
        for unit, _affinity in companions:
            selected.append((unit, bundle["id"]))
            if len(selected) >= max(0, int(max_companions or 0)):
                return selected
    return selected


def validate_experience_bundles(bundles, units):
    """Reject stale, forged, or content-bearing derived experience bundles."""
    if bundles is None:
        return
    if not isinstance(bundles, list):
        raise ValueError("experience bundles must be a list")
    units_by_id = {
        unit.get("id"): unit
        for unit in units or []
        if isinstance(unit, dict) and unit.get("id")
    }
    bundle_fields = {
        "id",
        "project",
        "session_ref",
        "date",
        "memory_types",
        "members",
    }
    member_fields = {
        "id",
        "revision",
        "type",
        "project",
        "date",
        "session_ref",
        "authority_role",
    }
    required_member_fields = member_fields - {"authority_role"}
    seen_bundle_ids = set()
    for bundle in bundles:
        if not isinstance(bundle, dict) or set(bundle) != bundle_fields:
            raise ValueError("experience bundle has an invalid shape")
        project = str(bundle.get("project") or "")
        session_ref = str(bundle.get("session_ref") or "")
        if (
            not project
            or canonical_project(project) != project
            or _session_refs({"source_refs": [session_ref]}) != [session_ref]
            or bundle.get("id") != _bundle_id(project, session_ref)
            or bundle["id"] in seen_bundle_ids
            or not isinstance(bundle.get("date"), str)
        ):
            raise ValueError("experience bundle identity is invalid")
        seen_bundle_ids.add(bundle["id"])
        members = bundle.get("members")
        if not isinstance(members, list) or len(members) < 2:
            raise ValueError("experience bundle requires at least two members")
        member_types = set()
        member_ids = set()
        for member in members:
            if (
                not isinstance(member, dict)
                or not required_member_fields.issubset(member)
                or not set(member).issubset(member_fields)
                or not is_valid_memory_id(member.get("id"))
                or member.get("type") not in RUNTIME_MEMORY_TYPES
                or member.get("project") != project
                or member.get("session_ref") != session_ref
                or not isinstance(member.get("date"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(member.get("revision") or ""))
                or member.get("id") in member_ids
            ):
                raise ValueError("experience bundle member is invalid")
            current = units_by_id.get(member["id"])
            if (
                not current
                or current.get("revision") != member["revision"]
                or current.get("type") != member["type"]
                or current.get("project") != project
            ):
                raise ValueError("experience bundle member revision is stale")
            member_ids.add(member["id"])
            member_types.add(member["type"])
        if len(member_types) < 2 or bundle.get("memory_types") != sorted(member_types):
            raise ValueError("experience bundle memory types are invalid")


def _eligible_member(unit):
    if not is_valid_runtime_record(unit):
        return False
    if not str(unit.get("project") or "").strip():
        return False
    path = str(unit.get("path") or "").replace("\\", "/")
    if _NON_FORMAL_PATH_PATTERN.search(path):
        return False
    return bool(_session_refs(unit))


def _companion_affinity(unit, seeds):
    companion = _memory_features(unit)
    if not companion:
        return 0
    return max(
        (len(companion & _memory_features(seed)) for seed in seeds),
        default=0,
    )


def _memory_features(unit):
    project = str(unit.get("project") or "").casefold()
    values = [
        *(str(item or "") for item in unit.get("terms") or []),
        str(unit.get("title") or ""),
        str(unit.get("summary") or ""),
    ]
    features = set()
    for value in values:
        normalized = value.casefold()
        features.update(re.findall(r"[a-z0-9][a-z0-9_.+-]{2,}", normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            features.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return {
        item
        for item in features
        if item
        and item != project
        and item not in _GENERIC_AFFINITY_FEATURES
    }


def _session_refs(unit):
    refs = []
    for raw in unit.get("source_refs") or []:
        ref = str(raw or "").strip()
        if (
            ref.startswith("session:")
            and len(ref) > len("session:")
            and not re.search(r"[\x00-\x20\x7f]", ref)
            and ref not in refs
        ):
            refs.append(ref)
    return refs


def _member_ref(unit, session_ref):
    member = {
        "id": unit["id"],
        "revision": unit["revision"],
        "type": unit["type"],
        "project": unit["project"],
        "date": str(unit.get("date") or ""),
        "session_ref": session_ref,
    }
    if unit.get("authority_role"):
        member["authority_role"] = unit["authority_role"]
    return member


def _bundle_id(project, session_ref):
    payload = f"{project}\0{session_ref}".encode("utf-8")
    return "experience:" + hashlib.sha256(payload).hexdigest()[:24]
