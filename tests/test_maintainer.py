import glob
import os
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from maintainer import expire_rules, generate_learning_card, promote_beta_rules, run
from validate_frontmatter import validate_frontmatter


class MaintainerTests(unittest.TestCase):
    def test_weekly_maintenance_previews_or_applies_explicit_memory_expiry(self):
        with tempfile.TemporaryDirectory() as vault:
            os.makedirs(os.path.join(vault, "00-Rules"))
            cfg = {"vault_path": vault}
            preview = SimpleNamespace(
                memory_id="decision-due",
                applied=False,
            )
            applied = SimpleNamespace(
                memory_id="decision-due",
                applied=True,
            )

            with (
                patch("maintainer.sweep_expired", return_value=[preview]) as sweep,
                patch("maintainer.write_effectiveness_report") as report,
                patch(
                    "maintainer.refresh_promotion_proposals",
                    return_value={"proposals": 2, "written": 0, "paths": []},
                ) as promotion,
            ):
                preview_result = run(cfg, dry_run=True)
                sweep.assert_called_once_with(cfg, apply=False)
                report.assert_not_called()
                promotion.assert_called_once_with(vault, cfg, apply=False)
            with (
                patch("maintainer.sweep_expired", return_value=[applied]) as sweep,
                patch(
                    "maintainer.write_effectiveness_report",
                    return_value={"written": True},
                ) as report,
                patch(
                    "maintainer.refresh_promotion_proposals",
                    return_value={"proposals": 2, "written": 1, "paths": ["proposal"]},
                ) as promotion,
            ):
                applied_result = run(cfg, dry_run=False)
                sweep.assert_called_once_with(cfg, apply=True)
                report.assert_called_once_with(vault, cfg)
                promotion.assert_called_once_with(vault, cfg, apply=True)

            self.assertEqual(preview_result["formal_memories_due"], 1)
            self.assertEqual(preview_result["formal_memories_expired"], 0)
            self.assertEqual(applied_result["formal_memories_due"], 1)
            self.assertEqual(applied_result["formal_memories_expired"], 1)
            self.assertEqual(preview_result["promotion_proposals"], 2)
            self.assertEqual(preview_result["promotion_proposals_written"], 0)
            self.assertEqual(applied_result["promotion_proposals_written"], 1)
            self.assertTrue(applied_result["effectiveness_report_refreshed"])

    def test_untrusted_rule_id_stays_inside_inbox_and_frontmatter_is_valid(self):
        with tempfile.TemporaryDirectory() as vault:
            inbox = os.path.join(vault, "00-Rules", "_inbox")
            os.makedirs(inbox)
            learning = {
                "action": "new_rule",
                "suggested_rule_id": "../../outside",
                "suggested_rule_title": "Title with YAML: value",
                "root_cause": "quoted \"value\"\nwith newline",
                "principle": "Never trust generated paths",
                "suggested_rule_text": "Keep writes inside the inbox.",
                "impact": "high",
                "total_occurrences": 2,
                "projects_affected": ["demo"],
                "affected_errors": ["path-filesystem"],
            }

            card_id = generate_learning_card(vault, learning, dry_run=False)

            cards = glob.glob(os.path.join(inbox, "*.md"))
            self.assertEqual(len(cards), 1)
            self.assertEqual(
                os.path.commonpath([os.path.realpath(inbox), os.path.realpath(cards[0])]),
                os.path.realpath(inbox),
            )
            self.assertFalse(os.path.exists(os.path.join(vault, "outside.md")))
            with open(cards[0], "r", encoding="utf-8") as handle:
                parts = handle.read().split("---", 2)
            frontmatter = yaml.safe_load(parts[1])
            self.assertEqual(frontmatter["rule_id"], "../../outside")
            self.assertEqual(frontmatter["status"], "pending")
            self.assertEqual(frontmatter["affected_projects"], ["demo"])
            self.assertEqual(frontmatter["one_liner"], "Title with YAML: value")
            ok, errors, template_type = validate_frontmatter(cards[0])
            self.assertTrue(ok, errors)
            self.assertEqual(template_type, "inbox_card")
            self.assertTrue(card_id.startswith("inbox-"))

    def test_expired_rule_is_backed_up_and_archived_with_updated_status(self):
        with tempfile.TemporaryDirectory() as vault:
            rules_dir = os.path.join(vault, "00-Rules")
            os.makedirs(rules_dir)
            rule_path = os.path.join(rules_dir, "RULE-OLD.md")
            write_rule(
                rule_path,
                {
                    "rule_id": "RULE-OLD",
                    "status": "active",
                    "expires": "2020-01-01",
                },
            )

            actions = expire_rules(rules_dir, dry_run=False)

            archived = os.path.join(rules_dir, "_archive", "RULE-OLD.md")
            backup = os.path.join(
                vault,
                "04-Feedback",
                "_rollback",
                datetime.now().strftime("%Y-%m-%d"),
                "00-Rules",
                "RULE-OLD.md",
            )
            self.assertTrue(actions)
            self.assertTrue(os.path.exists(archived))
            self.assertTrue(os.path.exists(backup))
            self.assertEqual(read_frontmatter(archived)["status"], "archived")
            self.assertFalse(os.path.exists(os.path.join(rules_dir, "04-Feedback")))

    def test_beta_promotion_backup_is_written_under_vault_rollback(self):
        with tempfile.TemporaryDirectory() as vault:
            rules_dir = os.path.join(vault, "00-Rules")
            os.makedirs(rules_dir)
            rule_path = os.path.join(rules_dir, "RULE-BETA.md")
            write_rule(
                rule_path,
                {
                    "rule_id": "RULE-BETA",
                    "status": "beta",
                    "beta_since": "2020-01-01",
                },
            )

            actions = promote_beta_rules(rules_dir, dry_run=False)

            backup = os.path.join(
                vault,
                "04-Feedback",
                "_rollback",
                datetime.now().strftime("%Y-%m-%d"),
                "00-Rules",
                "RULE-BETA.md",
            )
            self.assertTrue(actions)
            self.assertTrue(os.path.exists(backup))
            self.assertEqual(read_frontmatter(rule_path)["status"], "active")
            self.assertFalse(os.path.exists(os.path.join(rules_dir, "04-Feedback")))


def write_rule(path, frontmatter):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n")
        yaml.dump(frontmatter, handle, allow_unicode=True, sort_keys=False)
        handle.write("---\n\n# Rule\n")


def read_frontmatter(path):
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle.read().split("---", 2)[1])


if __name__ == "__main__":
    unittest.main()
