r"""Drive the keyboard lights from Cowork sessions by tailing their transcript.

Cowork (Claude desktop's local-agent-mode) ignores ~/.claude/settings.json, so
the hook mechanism never fires there, and its tools run inside a Linux sandbox
that cannot reach a Windows HID device. This watcher sidesteps both problems:
it runs on Windows, tails the transcript JSONL that Cowork writes live, and
feeds the same state machine the hooks use.

    python .\watch_cowork.py              # run until Ctrl+C
    python .\watch_cowork.py --dry-run    # print derived events, touch no lights
    python .\watch_cowork.py --replay FILE --dry-run   # validate against a file

Caveat: the transcript is an internal format with no compatibility promise.
If a Claude update changes it, the mapping in `derive_events` is what to fix.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterator


# Seconds a tool_use may stay unanswered before we assume it is waiting on the
# user rather than still executing. Cowork writes nothing while a permission
# prompt is open, so this gap is the only signal that one is showing.
APPROVAL_AFTER = 6.0
# No new transcript lines for this long ends the session and restores baseline.
IDLE_ENDS_SESSION = 180.0
POLL_INTERVAL = 0.4
RESCAN_INTERVAL = 10.0


def transcript_roots() -> list[Path]:
    """Places Claude may keep session transcripts, most specific first.

    The layout is not documented and has moved between releases, so probe
    several candidates instead of hard-coding one.
    """
    roots: list[Path] = []
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local)
        # Observed on Windows: the desktop app lives under %LOCALAPPDATA%,
        # not %APPDATA%, and splits data across several sibling folders.
        for name in ("Claude", "Claude-Data", "Claude-3p"):
            roots.append(base / name / "local-agent-mode-sessions")
            roots.append(base / name)
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Claude")
    roots += [home / ".claude" / "projects", home / ".claude", home / "Claude"]
    # Keep parents as well as their specific subdirectories: newest_transcript
    # returns at the first root that yields anything, so a broad parent is only
    # walked when the narrow path under it turned up empty.
    kept: list[Path] = []
    for root in roots:
        if root.exists() and root not in kept:
            kept.append(root)
    return kept


def newest_transcript(roots: list[Path]) -> Path | None:
    """Newest .jsonl from the first root that has any — roots are ordered
    most-specific first, so this avoids walking the whole app directory."""
    for root in roots:
        newest: Path | None = None
        newest_mtime = -1.0
        try:
            candidates = list(root.rglob("*.jsonl"))
        except OSError:
            continue
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = path, mtime
        if newest is not None:
            return newest
    return None


class TranscriptTail:
    """Yield newly appended JSON objects, tolerating partial trailing lines."""

    def __init__(self, path: Path, from_start: bool = False) -> None:
        self.path = path
        self.buffer = ""
        self.offset = 0 if from_start else self._size()

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def read_new(self) -> Iterator[dict[str, Any]]:
        size = self._size()
        if size < self.offset:  # File replaced or truncated.
            self.offset = 0
            self.buffer = ""
        if size == self.offset:
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except OSError:
            return
        self.buffer += chunk
        *complete, self.buffer = self.buffer.split("\n")
        for line in complete:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


class SessionState:
    """Turn transcript rows into the light states the daemon understands."""

    def __init__(self) -> None:
        self.pending: dict[str, float] = {}  # tool_use id -> first seen
        self.session_id = "cowork"
        self.last_row = time.time()
        self.turn_active = False
        self.reported: str | None = None

    def observe(self, row: dict[str, Any]) -> list[str]:
        """Return the events to emit for this row."""
        self.last_row = time.time()
        events: list[str] = []
        if row.get("sessionId"):
            self.session_id = str(row["sessionId"])

        # A subagent's rows describe the same visible work; don't double count.
        row_type = row.get("type")
        message = row.get("message") or {}
        content = message.get("content")
        blocks = content if isinstance(content, list) else []

        if row.get("toolDenialKind"):
            events.append("PostToolUseFailure")

        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use" and block.get("id"):
                self.pending[str(block["id"])] = time.time()
            elif kind == "tool_result" and block.get("tool_use_id"):
                self.pending.pop(str(block["tool_use_id"]), None)

        if row_type == "user" and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
        ):
            # A real prompt from the person, not a tool result being fed back.
            self.turn_active = True
            events.append("UserPromptSubmit")
        elif row_type == "assistant":
            if message.get("stop_reason") == "end_turn":
                self.turn_active = False
                self.pending.clear()
                events.append("Stop")
            else:
                self.turn_active = True
                events.append("PreToolUse")
        return events

    def idle_event(self) -> str | None:
        """Event implied by the passage of time rather than by a new row."""
        now = time.time()
        if self.turn_active and self.pending:
            oldest = min(self.pending.values())
            if now - oldest >= APPROVAL_AFTER:
                return "PermissionRequest"
        if now - self.last_row >= IDLE_ENDS_SESSION:
            return "SessionEnd"
        return None


def derive_events(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Pure mapping used by --replay so the logic can be tested off-Windows."""
    state = SessionState()
    out: list[tuple[str, str]] = []
    for row in rows:
        for event in state.observe(row):
            out.append((str(row.get("timestamp", "")), event))
    return out


def run(dry_run: bool, replay: Path | None) -> int:
    record_event = None
    if not dry_run:
        from keyboard_lights import record_event as _record

        record_event = _record

    if replay is not None:
        rows = []
        for line in replay.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        events = derive_events(rows)
        print(f"回放 {replay.name}：{len(rows)} 行 -> {len(events)} 个事件")
        counts: dict[str, int] = {}
        for _, event in events:
            counts[event] = counts.get(event, 0) + 1
        for event, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {event:<20} {count}")
        print("\n最后 12 个事件：")
        for stamp, event in events[-12:]:
            print(f"  {stamp}  {event}")
        return 0

    roots = transcript_roots()
    if not roots:
        print("找不到任何 Claude 数据目录。确认桌面版已安装并至少开过一次会话。")
        print("候选路径：%APPDATA%\\Claude、%LOCALAPPDATA%\\Claude、~\\.claude\\projects")
        return 1
    print("搜索目录：")
    for root in roots:
        print(f"  {root}")

    first = newest_transcript(roots)
    if first is None:
        print("\n这些目录下没有任何 .jsonl。手动找一下实际位置：")
        print('  Get-ChildItem "$env:APPDATA\\Claude" -Recurse -Filter *.jsonl | '
              "Sort-Object LastWriteTime -Descending | Select-Object -First 5 FullName")
        return 1
    print(f"\n最新 transcript：{first}")

    current: Path | None = None
    tail: TranscriptTail | None = None
    state = SessionState()
    last_scan = 0.0
    last_emitted: str | None = None

    def emit(event: str) -> None:
        nonlocal last_emitted
        if event == last_emitted and event in {"PreToolUse", "PermissionRequest"}:
            return  # Avoid hammering the state file with identical events.
        last_emitted = event
        stamp = time.strftime("%H:%M:%S")
        print(f"  {stamp}  {event}")
        if record_event is not None:
            try:
                record_event("claude", event, {"session_id": state.session_id})
            except Exception as exc:  # A light must never crash the watcher.
                print(f"  {stamp}  record_event 失败: {exc!r}")

    print("按 Ctrl+C 退出。\n")
    try:
        while True:
            now = time.time()
            if now - last_scan >= RESCAN_INTERVAL:
                last_scan = now
                newest = newest_transcript(roots)
                if newest is not None and newest != current:
                    current = newest
                    tail = TranscriptTail(newest)
                    state = SessionState()
                    last_emitted = None
                    print(f"切换到会话：{newest.name}")

            if tail is not None:
                for row in tail.read_new():
                    for event in state.observe(row):
                        emit(event)
                idle = state.idle_event()
                if idle is not None:
                    emit(idle)
                    if idle == "SessionEnd":
                        state.turn_active = False
                        state.last_row = time.time()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n已停止。")
        if record_event is not None:
            try:
                record_event("claude", "SessionEnd", {"session_id": state.session_id})
            except Exception:
                pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把 Cowork 会话活动映射到键盘灯效")
    parser.add_argument("--dry-run", action="store_true", help="只打印事件，不点灯")
    parser.add_argument("--replay", type=Path, help="离线回放一个 transcript 文件")
    args = parser.parse_args()
    return run(dry_run=args.dry_run or args.replay is not None, replay=args.replay)


if __name__ == "__main__":
    raise SystemExit(main())
