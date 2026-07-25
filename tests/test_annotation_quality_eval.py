import json
import os
import sys
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from evaluate_annotation_quality import evaluate_cases, load_cases


FIXTURE = os.path.join(
    REPO_ROOT,
    "tests",
    "fixtures",
    "annotation_quality",
    "cases.json",
)


class AnnotationQualityEvaluationTests(unittest.TestCase):
    def test_versioned_labeled_cases_meet_quality_gates(self):
        cases = load_cases(FIXTURE)
        report = evaluate_cases(cases)

        self.assertEqual(report["case_count"], 32)
        self.assertGreaterEqual(report["accuracy"], 0.94)
        self.assertGreaterEqual(report["formal_precision"], 0.95)
        self.assertGreaterEqual(report["formal_recall"], 0.95)
        self.assertEqual(report["unknown_predictions"], 0)

        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("API Key 无效", encoded)


if __name__ == "__main__":
    unittest.main()
