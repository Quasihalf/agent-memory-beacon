#!/usr/bin/env python3
"""Probe Codex Memory and compare reproducible black-box arm reports."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
from pathlib import Path


SCHEMA_VERSION = "1.0"
BEACON_CLAIM_SCORE = 85.0
BEACON_CLAIM_LEAD = 15.0
LATENCY_LIMIT_MS = 500.0
TOKEN_LIMIT = 1500.0
RATIO_METRICS = frozenset(
    {
        "precision_at_k",
        "critical_error_recall",
        "irrelevant_trigger_rate",
        "long_task_freshness_rate",
    }
)
COUNT_METRICS = frozenset({"contamination_count"})
COST_METRICS = frozenset({"recall_p95_ms", "max_estimated_tokens"})
REQUIRED_METRICS = RATIO_METRICS | COUNT_METRICS | COST_METRICS
ARM_FIELDS = frozenset(
    {
        "schema_version",
        "arm",
        "codex_version",
        "fixture_id",
        "fixture_sha256",
        "evidence_status",
        "metrics",
        "evidence_refs",
    }
)
PROBE_FIELDS = frozenset(
    {
        "schema_version",
        "probe_type",
        "codex_version",
        "feature_maturity",
        "feature_enabled",
        "memory_store_exists",
        "job_count",
        "stage1_output_count",
        "available",
        "unavailable_reasons",
    }
)
EVIDENCE_STATUSES = frozenset({"valid", "invalid", "unavailable"})
NON_SCORED_BEACON_CAPABILITIES = (
    "user_owned_obsidian_source",
    "candidate_isolation",
    "source_audit_trail",
    "formal_retract_and_supersede",
    "cross_agent_portability",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"\bcodex(?:-cli)?\s+([0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?)\b",
    re.IGNORECASE,
)


class ComparisonContractError(ValueError):
    """Raised when comparison evidence does not satisfy the fixed contract."""


def probe_codex_memory(
    *,
    codex_bin="codex",
    memory_db="~/.codex/memories_1.sqlite",
    runner=subprocess.run,
):
    """Inspect local Codex Memory capability without changing feature state."""
    reasons = set()
    codex_version = ""
    feature_maturity = "unknown"
    feature_enabled = None

    version_result = _run_codex(runner, [os.fspath(codex_bin), "--version"])
    if version_result is None:
        reasons.add("codex_unavailable")
    elif version_result.returncode != 0:
        reasons.add("codex_version_probe_failed")
    else:
        match = _VERSION_RE.search(_process_text(version_result.stdout))
        if match:
            codex_version = match.group(1)
        else:
            reasons.add("codex_version_unknown")

    feature_result = _run_codex(
        runner,
        [os.fspath(codex_bin), "features", "list"],
    )
    if feature_result is None:
        reasons.add("codex_unavailable")
    elif feature_result.returncode != 0:
        reasons.add("feature_probe_failed")
    else:
        parsed = _parse_memory_feature(_process_text(feature_result.stdout))
        if parsed is None:
            reasons.add("memory_feature_unreported")
        else:
            feature_maturity, feature_enabled = parsed
            if not feature_enabled:
                reasons.add("feature_disabled")

    db_path = Path(memory_db).expanduser()
    memory_store_exists = db_path.is_file()
    job_count = None
    stage1_output_count = None
    if not memory_store_exists:
        reasons.add("memory_store_missing")
    else:
        try:
            job_count, stage1_output_count = _read_memory_counts(db_path)
        except (OSError, sqlite3.Error):
            reasons.add("memory_store_unreadable")
        if stage1_output_count is None:
            reasons.add("memory_schema_unavailable")
        elif stage1_output_count == 0:
            reasons.add("empty_memory_store")

    blockers = {
        "codex_unavailable",
        "codex_version_probe_failed",
        "codex_version_unknown",
        "feature_probe_failed",
        "memory_feature_unreported",
        "feature_disabled",
        "memory_store_missing",
        "memory_store_unreadable",
        "memory_schema_unavailable",
        "empty_memory_store",
    }
    available = bool(
        feature_enabled is True
        and memory_store_exists
        and stage1_output_count is not None
        and stage1_output_count > 0
        and not (reasons & blockers)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "probe_type": "codex-memory-capability",
        "codex_version": codex_version,
        "feature_maturity": feature_maturity,
        "feature_enabled": feature_enabled,
        "memory_store_exists": memory_store_exists,
        "job_count": job_count,
        "stage1_output_count": stage1_output_count,
        "available": available,
        "unavailable_reasons": sorted(reasons),
    }


def score_arm_report(report):
    """Score one valid behavioral arm using only the approved dimensions."""
    _validate_arm_report(report)
    if report["evidence_status"] != "valid":
        raise ComparisonContractError("arm evidence must be valid before scoring")
    metrics = report["metrics"]
    dimensions = {
        "relevance_accuracy": 30.0 * metrics["precision_at_k"],
        "critical_error_recall": 25.0 * metrics["critical_error_recall"],
        "pollution_control": (
            10.0 * (1.0 - metrics["irrelevant_trigger_rate"])
            + (10.0 if metrics["contamination_count"] == 0 else 0.0)
        ),
        "long_task_freshness": 15.0 * metrics["long_task_freshness_rate"],
        "latency_and_context_cost": (
            5.0 * _remaining_efficiency(metrics["recall_p95_ms"], LATENCY_LIMIT_MS)
            + 5.0
            * _remaining_efficiency(
                metrics["max_estimated_tokens"],
                TOKEN_LIMIT,
            )
        ),
    }
    dimensions = {
        name: round(value, 6) for name, value in dimensions.items()
    }
    return {
        "total": round(sum(dimensions.values()), 6),
        "dimensions": dimensions,
    }


def compare_memory_reports(beacon_report, codex_report, codex_probe):
    """Compare two same-version arms, or return an honest N/A result."""
    _validate_probe(codex_probe)
    _validate_arm_report(beacon_report, expected_arm="beacon")
    common = {
        "schema_version": SCHEMA_VERSION,
        "comparison_type": "same-version-black-box",
        "codex_version": codex_probe.get("codex_version")
        or beacon_report["codex_version"],
        "fixture_id": beacon_report["fixture_id"],
        "fixture_sha256": beacon_report["fixture_sha256"],
        "claim_thresholds": {
            "beacon_minimum_score": BEACON_CLAIM_SCORE,
            "minimum_score_lead": BEACON_CLAIM_LEAD,
        },
        "non_scored_beacon_capabilities": list(
            NON_SCORED_BEACON_CAPABILITIES
        ),
    }

    if beacon_report["evidence_status"] != "valid":
        return _na_result(
            common,
            reasons=["beacon_evidence_not_valid"],
        )

    beacon_scorecard = score_arm_report(beacon_report)
    if not codex_probe["available"]:
        return _na_result(
            common,
            reasons=codex_probe["unavailable_reasons"]
            or ["codex_memory_unavailable"],
            beacon_scorecard=beacon_scorecard,
        )
    if codex_report is None:
        return _na_result(
            common,
            reasons=["codex_arm_report_missing"],
            beacon_scorecard=beacon_scorecard,
        )

    _validate_arm_report(codex_report, expected_arm="codex_memory")
    if codex_report["evidence_status"] != "valid":
        return _na_result(
            common,
            reasons=["codex_evidence_not_valid"],
            beacon_scorecard=beacon_scorecard,
        )
    if beacon_report["codex_version"] != codex_report["codex_version"]:
        raise ComparisonContractError(
            "both arms must use the same Codex version"
        )
    if codex_probe["codex_version"] != codex_report["codex_version"]:
        raise ComparisonContractError(
            "probe version must match the compared Codex version"
        )
    if (
        beacon_report["fixture_id"] != codex_report["fixture_id"]
        or beacon_report["fixture_sha256"]
        != codex_report["fixture_sha256"]
    ):
        raise ComparisonContractError("both arms must use the same fixture")

    codex_scorecard = score_arm_report(codex_report)
    beacon_score = beacon_scorecard["total"]
    codex_score = codex_scorecard["total"]
    delta = round(beacon_score - codex_score, 6)
    claim_allowed = bool(
        beacon_score >= BEACON_CLAIM_SCORE and delta >= BEACON_CLAIM_LEAD
    )
    return {
        **common,
        "status": "valid",
        "claim_allowed": claim_allowed,
        "verdict": (
            "beacon_exceeds_codex_memory"
            if claim_allowed
            else "claim_gate_not_met"
        ),
        "beacon_score": beacon_score,
        "codex_memory_score": codex_score,
        "score_delta": delta,
        "beacon_scorecard": beacon_scorecard,
        "codex_memory_scorecard": codex_scorecard,
        "unavailable_reasons": [],
    }


def _na_result(common, *, reasons, beacon_scorecard=None):
    return {
        **common,
        "status": "N/A",
        "claim_allowed": False,
        "verdict": "comparison_unavailable",
        "beacon_score": (
            beacon_scorecard["total"] if beacon_scorecard is not None else None
        ),
        "codex_memory_score": None,
        "score_delta": None,
        "beacon_scorecard": beacon_scorecard,
        "codex_memory_scorecard": None,
        "unavailable_reasons": sorted(set(reasons)),
    }


def _validate_arm_report(report, expected_arm=None):
    if not isinstance(report, dict):
        raise ComparisonContractError("arm report must be a JSON object")
    fields = frozenset(report)
    if fields != ARM_FIELDS:
        missing = sorted(ARM_FIELDS - fields)
        extra = sorted(fields - ARM_FIELDS)
        raise ComparisonContractError(
            f"arm report schema drift: missing={missing}, extra={extra}"
        )
    if report["schema_version"] != SCHEMA_VERSION:
        raise ComparisonContractError("unsupported arm report schema version")
    if report["arm"] not in {"beacon", "codex_memory"}:
        raise ComparisonContractError("arm must be beacon or codex_memory")
    if expected_arm and report["arm"] != expected_arm:
        raise ComparisonContractError(f"expected {expected_arm} arm report")
    for field in ("codex_version", "fixture_id"):
        if not isinstance(report[field], str) or not report[field].strip():
            raise ComparisonContractError(f"{field} must be a non-empty string")
    if not isinstance(report["fixture_sha256"], str) or not _SHA256_RE.fullmatch(
        report["fixture_sha256"]
    ):
        raise ComparisonContractError("fixture_sha256 must be lowercase SHA-256")
    if report["evidence_status"] not in EVIDENCE_STATUSES:
        raise ComparisonContractError("unsupported evidence_status")
    refs = report["evidence_refs"]
    if not isinstance(refs, list) or any(
        not isinstance(item, str) or not item.strip() for item in refs
    ):
        raise ComparisonContractError("evidence_refs must be a string list")
    if report["evidence_status"] == "valid" and not refs:
        raise ComparisonContractError("valid evidence requires evidence_refs")

    metrics = report["metrics"]
    if not isinstance(metrics, dict) or frozenset(metrics) != REQUIRED_METRICS:
        raise ComparisonContractError(
            "behavioral metric drift: expected exactly "
            + ", ".join(sorted(REQUIRED_METRICS))
        )
    for name in RATIO_METRICS:
        value = _finite_number(metrics[name], name)
        if not 0.0 <= value <= 1.0:
            raise ComparisonContractError(f"{name} must be between 0 and 1")
    for name in COST_METRICS:
        value = _finite_number(metrics[name], name)
        if value < 0:
            raise ComparisonContractError(f"{name} must be non-negative")
    count = metrics["contamination_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ComparisonContractError(
            "contamination_count must be a non-negative integer"
        )


def _validate_probe(probe):
    if not isinstance(probe, dict):
        raise ComparisonContractError("Codex Memory probe must be a JSON object")
    fields = frozenset(probe)
    if fields != PROBE_FIELDS:
        missing = sorted(PROBE_FIELDS - fields)
        extra = sorted(fields - PROBE_FIELDS)
        raise ComparisonContractError(
            f"probe schema drift: missing={missing}, extra={extra}"
        )
    if probe["schema_version"] != SCHEMA_VERSION:
        raise ComparisonContractError("unsupported probe schema version")
    if probe["probe_type"] != "codex-memory-capability":
        raise ComparisonContractError("unexpected probe_type")
    for field in ("codex_version", "feature_maturity"):
        if not isinstance(probe[field], str):
            raise ComparisonContractError(f"probe {field} must be a string")
    if probe["feature_enabled"] is not None and not isinstance(
        probe["feature_enabled"], bool
    ):
        raise ComparisonContractError("probe feature_enabled must be boolean or null")
    for field in ("memory_store_exists", "available"):
        if not isinstance(probe[field], bool):
            raise ComparisonContractError(f"probe {field} must be boolean")
    for field in ("job_count", "stage1_output_count"):
        value = probe[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ComparisonContractError(
                f"probe {field} must be a non-negative integer or null"
            )
    reasons = probe["unavailable_reasons"]
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ):
        raise ComparisonContractError(
            "probe unavailable_reasons must be a string list"
        )
    if probe["available"] and (
        probe["feature_enabled"] is not True
        or not probe["memory_store_exists"]
        or not probe["stage1_output_count"]
        or reasons
        or not probe["codex_version"]
    ):
        raise ComparisonContractError("available probe has inconsistent evidence")


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparisonContractError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ComparisonContractError(f"{name} must be finite")
    return value


def _remaining_efficiency(value, limit):
    return max(0.0, 1.0 - float(value) / limit)


def _run_codex(runner, args):
    try:
        return runner(
            args,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def _process_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _parse_memory_feature(output):
    for raw_line in output.splitlines():
        fields = raw_line.split()
        if len(fields) < 3 or fields[0] != "memories":
            continue
        state = fields[-1].lower()
        if state not in {"true", "false"}:
            return None
        return " ".join(fields[1:-1]), state == "true"
    return None


def _read_memory_counts(path):
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        job_count = (
            connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            if "jobs" in tables
            else None
        )
        stage1_count = (
            connection.execute(
                "SELECT COUNT(*) FROM stage1_outputs"
            ).fetchone()[0]
            if "stage1_outputs" in tables
            else None
        )
        return int(job_count) if job_count is not None else None, (
            int(stage1_count) if stage1_count is not None else None
        )
    finally:
        connection.close()


def _load_json(path, label):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComparisonContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ComparisonContractError(f"{label} must contain a JSON object")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe and compare Agent Memory Beacon with Codex Memory"
    )
    parser.add_argument("--beacon-report", default="")
    parser.add_argument("--codex-report", default="")
    parser.add_argument("--codex-probe", default="")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--memory-db",
        default="~/.codex/memories_1.sqlite",
    )
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args(argv)

    try:
        probe = (
            _load_json(args.codex_probe, "Codex probe")
            if args.codex_probe
            else probe_codex_memory(
                codex_bin=args.codex_bin,
                memory_db=args.memory_db,
            )
        )
        if args.probe_only:
            _validate_probe(probe)
            payload = probe
        else:
            if not args.beacon_report:
                raise ComparisonContractError("--beacon-report is required")
            beacon = _load_json(args.beacon_report, "Beacon arm report")
            codex = (
                _load_json(args.codex_report, "Codex Memory arm report")
                if args.codex_report
                else None
            )
            payload = compare_memory_reports(beacon, codex, probe)
    except ComparisonContractError as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
