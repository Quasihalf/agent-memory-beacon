#!/usr/bin/env python3
"""Fail-open Codex UserPromptSubmit adapter for Agent Memory Beacon."""
from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from typing import Mapping

from config import load_config
from memory_runtime import HookResult, PromptEvent, handle_prompt


MAX_STDIN_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 256 * 1024
MAX_SESSION_KEY_BYTES = 4096
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_payload(payload) -> PromptEvent | None:
    if not isinstance(payload, Mapping):
        return None
    event_name = str(
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event_name")
        or "UserPromptSubmit"
    ).strip()
    if event_name != "UserPromptSubmit":
        return None

    prompt = _prompt_from_payload(payload)
    if not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        return None

    session_key = _session_key_from_payload(payload)
    if not session_key:
        return None

    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        workspace = payload.get("workspace")
        cwd = workspace.get("cwd") if isinstance(workspace, Mapping) else ""
    cwd = str(cwd or "").strip()
    if cwd and not os.path.isabs(cwd):
        cwd = ""

    return PromptEvent(
        session_key=session_key,
        prompt=prompt,
        cwd=cwd,
        event_name="UserPromptSubmit",
        agent="codex",
    )


def build_hook_output(result: HookResult) -> dict:
    context = str(getattr(result, "additional_context", "") or "")
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def main(
    *,
    stdin=None,
    stdout=None,
    config_loader=load_config,
    runtime_handler=handle_prompt,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    output = {}
    try:
        raw = stdin.read(MAX_STDIN_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
            raise ValueError("Hook input exceeds size limit")
        payload = json.loads(raw)
        event = normalize_payload(payload)
        if event is not None:
            captured = io.StringIO()
            with redirect_stdout(captured):
                cfg = config_loader()
                result = runtime_handler(event, cfg)
            output = build_hook_output(result)
    except BaseException:
        output = {}
    stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    stdout.write("\n")
    return 0


def _prompt_from_payload(payload: Mapping[str, object]) -> str:
    for key in ("prompt", "user_prompt", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container_key in ("input", "hook_input", "payload"):
        container = payload.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for key in ("prompt", "user_prompt", "text", "message"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _session_key_from_payload(payload: Mapping[str, object]) -> str:
    for field, prefix in (
        ("session_id", "session"),
        ("thread_id", "thread"),
        ("conversation_id", "conversation"),
    ):
        value = payload.get(field)
        if _valid_identifier(value):
            return f"{prefix}:{str(value).strip()}"
    transcript_path = payload.get("transcript_path")
    if (
        isinstance(transcript_path, str)
        and os.path.isabs(transcript_path)
        and _valid_identifier(transcript_path)
    ):
        return f"transcript:{os.path.normpath(transcript_path)}"
    return ""


def _valid_identifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(
        text
        and not CONTROL_CHARS.search(text)
        and len(text.encode("utf-8")) <= MAX_SESSION_KEY_BYTES
    )


if __name__ == "__main__":
    raise SystemExit(main())
