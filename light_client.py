"""Append-only hook client for the unified light service.

A hook process does exactly one thing: normalize the hook payload into a
single JSON line and append it to events.jsonl. It never touches the HID
device, never starts or probes a daemon, and never reads shared state to
decide a light. Appends to a pre-existing file are the one write pattern
measured to cross both the Codex and the Claude sandbox (state.json got
copy-on-write views with different inodes on each side; the append log kept
one inode for everyone), which is why the whole protocol is an append.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MachenikeTaskLights"
EVENTS_FILE = APP_DIR / "events.jsonl"
EVENTS_MUTEX = "Local\\MachenikeTaskLights-Events-v1"
SCHEMA = 1
# One event must stay one atomic line well under a pipe/sector boundary.
MAX_LINE_BYTES = 4096

SESSION_KEYS = ("session_id", "thread_id", "conversation_id", "task_id")
AGENT_KEYS = ("agent_id", "subagent_id", "agent_session_id")


def _named_mutex_acquire(name: str, timeout_ms: int = 3000):
    """Return a held mutex handle, or None when unavailable (non-fatal)."""
    if os.name != "nt":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    WAIT_OBJECT_0 = 0
    WAIT_ABANDONED = 0x80
    result = kernel32.WaitForSingleObject(handle, timeout_ms)
    if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
        kernel32.CloseHandle(handle)
        return None
    return (kernel32, handle)


def _named_mutex_release(held) -> None:
    if held is None:
        return
    kernel32, handle = held
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


def _read_stdin_text() -> str:
    """Read the hook payload as UTF-8 rather than the console codepage.

    Windows hands a piped stdin the ANSI codepage -- gbk on a Chinese install
    -- but hook payloads are UTF-8 JSON. Decoding UTF-8 bytes as gbk can emit
    a stray 0x5C backslash, which breaks the JSON string escapes: measured on
    2026-09-01, every payload naming a non-ASCII path (this repo's own
    directory) failed to parse. Those events reached the service with no
    session_id and were then attached to whichever session of that source was
    touched last -- so a Stop from one project could end a *different*
    project's marquee and leave the real one running until its lease expired.
    """
    try:
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            return buffer.read().decode("utf-8", errors="replace")
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def read_hook_payload(extra_args: list[str] | None = None) -> dict[str, Any]:
    """Parse the hook JSON from argv leftovers or stdin."""
    candidates = [arg for arg in (extra_args or []) if arg.lstrip().startswith("{")]
    try:
        if not sys.stdin.isatty():
            incoming = _read_stdin_text()
            if incoming.strip():
                candidates.append(incoming)
    except (OSError, ValueError):
        pass
    for candidate in reversed(candidates):
        try:
            # A PowerShell pipe prepends a UTF-8 BOM; json.loads chokes on it.
            parsed = json.loads(candidate.lstrip("﻿"))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {}


def _classify_notification(payload: dict[str, Any]) -> str | None:
    """Reduce a notification to a category so no message text is recorded."""
    hints = " ".join(
        str(payload.get(key, ""))
        for key in ("notification_type", "message", "title")
    ).lower()
    if "permission" in hints or "approval" in hints or "授权" in hints:
        return "permission"
    if "idle" in hints or "waiting" in hints or "input" in hints:
        return "idle"
    if "error" in hints or "fail" in hints:
        return "error"
    return None


def build_event(source: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "event_id": str(uuid.uuid4()),
        "recorded_at": round(time.time(), 3),
        "source": source,
        "event": event,
    }
    for key in SESSION_KEYS:
        value = payload.get(key)
        if value:
            record["session_id"] = str(value)
            break
    else:
        cwd = payload.get("cwd")
        if cwd:
            # A path can name a private project; a short digest still gives a
            # stable per-directory session key without recording the path.
            digest = hashlib.sha1(str(cwd).encode("utf-8")).hexdigest()[:12]
            record["session_id"] = f"cwd-{digest}"
    for key in AGENT_KEYS:
        value = payload.get(key)
        if value:
            record["agent_id"] = str(value)
            break
    if event.lower().replace("_", "") == "notification":
        category = _classify_notification(payload)
        if category:
            record["notification_type"] = category
    return record


def append_event(record: dict[str, Any], events_file: Path | None = None) -> None:
    target = events_file or EVENTS_FILE
    line = json.dumps(record, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        # Optional fields only; the mandatory ones are all short.
        for key in ("notification_type", "agent_id", "session_id"):
            record.pop(key, None)
            line = json.dumps(record, ensure_ascii=False)
            if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
                break
    target.parent.mkdir(parents=True, exist_ok=True)
    held = _named_mutex_acquire(EVENTS_MUTEX)
    try:
        # One write() for the whole line, flushed to disk before returning, so
        # the service's reader never sees half an event from this process.
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        _named_mutex_release(held)


def handle_hook(source: str, event: str, extra_args: list[str] | None = None) -> int:
    """Full hook client: read stdin, append one event, exit. Never raises."""
    try:
        payload = read_hook_payload(extra_args)
        append_event(build_event(source, event, payload))
    except Exception as exc:  # A light must never break the agent's own task.
        print(f"keyboard-lights: event append failed: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: light_client.py <source> <event> [payload-json]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(handle_hook(sys.argv[1], sys.argv[2], sys.argv[3:]))
