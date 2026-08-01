import io
import json
import os
import subprocess
import sys
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)


class CodexPromptHookTests(unittest.TestCase):
    def test_normalize_payload_supports_session_identifier_variants(self):
        from codex_prompt_hook import normalize_payload

        variants = (
            ({"session_id": "sess-1"}, "session:sess-1"),
            ({"thread_id": "thread-1"}, "thread:thread-1"),
            ({"conversation_id": "conversation-1"}, "conversation:conversation-1"),
            ({"transcript_path": "/tmp/session.jsonl"}, "transcript:/tmp/session.jsonl"),
        )
        for fields, expected in variants:
            with self.subTest(fields=fields):
                event = normalize_payload(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "检查动态召回",
                        "cwd": "/tmp/demo",
                        **fields,
                    }
                )
                self.assertEqual(event.session_key, expected)
                self.assertEqual(event.prompt, "检查动态召回")
                self.assertEqual(event.cwd, "/tmp/demo")
                self.assertEqual(event.event_name, "UserPromptSubmit")
                self.assertEqual(event.agent, "codex")

    def test_explicit_session_id_precedes_other_identifiers(self):
        from codex_prompt_hook import normalize_payload

        event = normalize_payload(
            {
                "session_id": "session-wins",
                "thread_id": "thread-loses",
                "conversation_id": "conversation-loses",
                "transcript_path": "/tmp/loses.jsonl",
                "prompt": "检查优先级",
            }
        )

        self.assertEqual(event.session_key, "session:session-wins")

    def test_prompt_and_cwd_variants_are_normalized(self):
        from codex_prompt_hook import normalize_payload

        variants = (
            ({"prompt": "prompt field", "cwd": "/tmp/one"}, "prompt field", "/tmp/one"),
            ({"user_prompt": "user field", "cwd": "/tmp/two"}, "user field", "/tmp/two"),
            (
                {"input": {"text": "nested text"}, "workspace": {"cwd": "/tmp/three"}},
                "nested text",
                "/tmp/three",
            ),
        )
        for fields, prompt, cwd in variants:
            with self.subTest(fields=fields):
                event = normalize_payload({"thread_id": "thread", **fields})
                self.assertEqual(event.prompt, prompt)
                self.assertEqual(event.cwd, cwd)

    def test_missing_reliable_session_key_never_uses_cwd(self):
        from codex_prompt_hook import normalize_payload

        self.assertIsNone(
            normalize_payload(
                {"prompt": "有内容", "cwd": "/tmp/shared-project"}
            )
        )
        self.assertIsNone(
            normalize_payload(
                {
                    "prompt": "有内容",
                    "transcript_path": "relative/session.jsonl",
                    "cwd": "/tmp/shared-project",
                }
            )
        )

    def test_invalid_event_prompt_and_identifier_are_rejected(self):
        from codex_prompt_hook import MAX_PROMPT_BYTES, normalize_payload

        invalid = (
            [],
            {"thread_id": "thread", "prompt": ""},
            {"thread_id": "bad\x00id", "prompt": "hello"},
            {"thread_id": "thread", "prompt": "x" * (MAX_PROMPT_BYTES + 1)},
            {
                "thread_id": "thread",
                "prompt": "hello",
                "hook_event_name": "Stop",
            },
        )
        for payload in invalid:
            with self.subTest(payload_type=type(payload).__name__):
                self.assertIsNone(normalize_payload(payload))

    def test_build_hook_output_emits_only_additional_context_contract(self):
        from codex_prompt_hook import build_hook_output
        from memory_runtime import HookResult

        self.assertEqual(build_hook_output(HookResult(status="silent")), {})
        self.assertEqual(
            build_hook_output(
                HookResult(
                    additional_context="[MEMORY_REFRESH]\nloaded: 1",
                    status="success",
                    loaded=1,
                )
            ),
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "[MEMORY_REFRESH]\nloaded: 1",
                }
            },
        )

    def test_build_hook_output_emits_summary_only_checkpoint_context(self):
        from codex_prompt_hook import build_hook_output
        from memory_runtime import HookResult

        self.assertEqual(
            build_hook_output(
                HookResult(
                    additional_context="[PRIVATE ROLLING SUMMARY CHECKPOINT]",
                    status="silent",
                    summary_requested=True,
                )
            ),
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "[PRIVATE ROLLING SUMMARY CHECKPOINT]",
                }
            },
        )

    def test_main_success_captures_internal_stdout_and_emits_one_json_line(self):
        from codex_prompt_hook import main
        from memory_runtime import HookResult

        stdin = io.StringIO(
            json.dumps(
                {
                    "thread_id": "thread-success",
                    "prompt": "检查动态召回",
                    "cwd": "/tmp/demo",
                }
            )
        )
        stdout = io.StringIO()

        def noisy_config_loader():
            print("internal config noise")
            return {"memory_runtime": {"enabled": True}}

        def noisy_handler(event, cfg):
            print("internal runtime noise")
            return HookResult(
                additional_context="[MEMORY_REFRESH]\nloaded: 1",
                status="success",
                loaded=1,
            )

        code = main(
            stdin=stdin,
            stdout=stdout,
            config_loader=noisy_config_loader,
            runtime_handler=noisy_handler,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        parsed = json.loads(stdout.getvalue())
        self.assertIn("hookSpecificOutput", parsed)
        self.assertNotIn("noise", stdout.getvalue())

    def test_main_runtime_exception_fails_open(self):
        from codex_prompt_hook import main

        stdin = io.StringIO(
            json.dumps({"session_id": "sess", "prompt": "检查异常"})
        )
        stdout = io.StringIO()

        def explode(*args, **kwargs):
            raise RuntimeError("secret failure detail")

        code = main(
            stdin=stdin,
            stdout=stdout,
            config_loader=lambda: {},
            runtime_handler=explode,
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {})
        self.assertNotIn("secret", stdout.getvalue())

    def test_subprocess_malformed_missing_and_oversized_input_fail_open(self):
        script = os.path.join(SCRIPTS_DIR, "codex_prompt_hook.py")
        cases = (
            "{not-json",
            json.dumps({"prompt": "missing session", "cwd": "/tmp/demo"}),
            "x" * (1024 * 1024 + 1),
        )
        for payload in cases:
            with self.subTest(size=len(payload)):
                completed = subprocess.run(
                    [sys.executable, script],
                    input=payload,
                    text=True,
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(json.loads(completed.stdout), {})
                self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
