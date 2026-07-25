#!/usr/bin/env python3
"""Evaluate deterministic annotation classification against labeled cases."""
import argparse
import json
import os

from annotation_quality import assess_decision, assess_error, assess_favor


ASSESSORS = {
    "decision": assess_decision,
    "error": assess_error,
    "favor": assess_favor,
}
EXPECTED_SCHEMA_VERSION = "1.0"


def load_cases(path):
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError("annotation quality fixture schema is invalid")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("annotation quality fixture has no cases")
    ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("annotation quality case must be an object")
        case_id = str(case.get("id") or "").strip()
        annotation_type = str(case.get("annotation_type") or "").strip()
        if not case_id or case_id in ids:
            raise ValueError("annotation quality case IDs must be unique")
        if annotation_type not in ASSESSORS:
            raise ValueError("annotation quality case type is invalid")
        if case.get("expected") not in {"formal", "candidate", "rejected"}:
            raise ValueError("annotation quality expected status is invalid")
        if not isinstance(case.get("payload"), dict):
            raise ValueError("annotation quality case payload must be an object")
        ids.add(case_id)
    return cases


def evaluate_cases(cases):
    labels = ("formal", "candidate", "rejected")
    confusion = {
        expected: {predicted: 0 for predicted in labels}
        for expected in labels
    }
    per_type = {}
    mismatches = []
    unknown_predictions = 0
    for case in cases:
        annotation_type = case["annotation_type"]
        expected = case["expected"]
        assessment = ASSESSORS[annotation_type](case["payload"])
        predicted = assessment.status
        if predicted not in labels:
            unknown_predictions += 1
            continue
        confusion[expected][predicted] += 1
        bucket = per_type.setdefault(annotation_type, {"total": 0, "correct": 0})
        bucket["total"] += 1
        if predicted == expected:
            bucket["correct"] += 1
        else:
            mismatches.append(
                {
                    "id": case["id"],
                    "annotation_type": annotation_type,
                    "expected": expected,
                    "predicted": predicted,
                    "reasons": list(assessment.reasons),
                }
            )

    total = len(cases)
    correct = sum(confusion[label][label] for label in labels)
    formal_true_positive = confusion["formal"]["formal"]
    formal_predicted = sum(confusion[label]["formal"] for label in labels)
    formal_actual = sum(confusion["formal"].values())
    return {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "case_count": total,
        "accuracy": _ratio(correct, total),
        "formal_precision": _ratio(formal_true_positive, formal_predicted),
        "formal_recall": _ratio(formal_true_positive, formal_actual),
        "unknown_predictions": unknown_predictions,
        "confusion_matrix": confusion,
        "per_type": {
            key: {
                **value,
                "accuracy": _ratio(value["correct"], value["total"]),
            }
            for key, value in sorted(per_type.items())
        },
        "mismatches": mismatches,
    }


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else 0.0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate DECISION/ERROR/FAVOR semantic classification"
    )
    parser.add_argument(
        "--cases",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests",
            "fixtures",
            "annotation_quality",
            "cases.json",
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate_cases(load_cases(args.cases))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Annotation quality: "
            f"accuracy={report['accuracy']:.2%}, "
            f"formal_precision={report['formal_precision']:.2%}, "
            f"formal_recall={report['formal_recall']:.2%}, "
            f"cases={report['case_count']}"
        )
        for item in report["mismatches"]:
            print(
                f"MISMATCH {item['id']}: expected={item['expected']} "
                f"predicted={item['predicted']}"
            )
    return 0 if (
        report["accuracy"] >= 0.94
        and report["formal_precision"] >= 0.95
        and report["formal_recall"] >= 0.95
        and report["unknown_predictions"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
