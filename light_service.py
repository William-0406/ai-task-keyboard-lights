"""The single resident light service for Codex + Claude keyboard hints.

Hooks append events to events.jsonl (see light_client.py); this service is
the only process that reads them for decisions and the only process allowed
to call send_packet(). It runs outside both agent sandboxes, so its state
machine lives in ordinary process memory — the cross-sandbox shared-state
problem that produced two divergent state.json views simply no longer has a
seat at the table.

The state machine itself is pure (no I/O, explicit clocks) so every rule in
the handoff spec is unit-testable without a keyboard.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

from light_client import APP_DIR, EVENTS_FILE

SERVICE_LOG = APP_DIR / "service.log"
SNAPSHOT_FILE = APP_DIR / "service-state.json"
SERVICE_MUTEX = "Local\\MachenikeTaskLights-Service-v1"
SERVICE_STOP_EVENT = "Local\\MachenikeTaskLights-ServiceStop-v1"

DONE_SECONDS = 10.0
ERROR_SECONDS = 10.0
# How long each colour holds when Codex and Claude are both working. Two
# seconds read as flicker in daily use; this is the one number to tune if the
# alternation feels too busy or too slow to notice.
BOTH_ALTERNATE_SECONDS = 5.0
# A "working" claim must be renewed by real activity (every tool call fires
# Pre/PostToolUse). The lease only exists so a crashed agent cannot pin the
# marquee on forever.
WORKING_LEASE = 30 * 60.0
# Events that arrive without a session id attach to the newest live session
# of the same source, provided it was touched this recently.
ADOPT_WINDOW = 30 * 60.0
# The desktop apps have been observed splitting ONE conversation across two
# session ids: UserPromptSubmit/tool events under id A, Stop under id B
# (measured 2026-08-31: work id 4b0370d3 never received a Stop; twin id
# 0871315c received nothing but Stops). Id A then stays "working" for the
# whole lease and the marquee outlives the answer by half an hour. So when a
# session of a source finishes, sibling *working* sessions of that source
# that have been inactive this long are stood down. A genuinely concurrent
# task refreshes on every tool call (seconds apart), so it is never this
# stale while alive — and even a false positive revives on its next event.
# Approval sessions are never touched: hiding a permission prompt is worse
# than a stale marquee.
STRANDED_AFTER = 120.0
# Idle/registered sessions that stop producing events entirely are dropped.
IDLE_PRUNE_AFTER = 6 * 60 * 60.0
# Replayed on every start; truncated in place (never renamed — renaming is
# what gave each sandbox a private copy of state.json) once it outgrows this
# and nothing is active.
EVENTS_COMPACT_BYTES = 20 * 1024 * 1024
DEDUP_CAPACITY = 8192

# error > approval > done > working > baseline
_STATUS_PRIORITY = ("error", "approval", "done")

# Events that END a session. An id-less one of these may not be guessed.
_TERMINAL_EVENTS = {"stop", "stopfailure", "sessionend"}

_ACTIVITY_EVENTS = {
    "userpromptsubmit",
    "pretooluse",
    "posttooluse",
    "posttoolusefailure",
}


class StateMachine:
    """All session state, in memory, keyed by (source, session, agent)."""

    def __init__(self) -> None:
        # key -> {"status", "source", "updated", "expires", "event"}
        self.sessions: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.last_event_id: str | None = None
        self.dropped_ambiguous = 0

    # -- helpers ---------------------------------------------------------

    def _dedup(self, event_id: str | None) -> bool:
        """True when this event was already processed."""
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        self._seen[event_id] = None
        while len(self._seen) > DEDUP_CAPACITY:
            self._seen.popitem(last=False)
        return False

    def _live_main_sessions(self, source: str, now: float) -> int:
        """How many main sessions of this source are currently unexpired."""
        count = 0
        for key, entry in self.sessions.items():
            if key[0] != source or key[2] is not None:
                continue
            expires = entry.get("expires")
            if expires is not None and float(expires) <= now:
                continue
            if entry.get("status") in {"working", "approval"}:
                count += 1
        return count

    def _resolve_sid(self, source: str, sid: str | None, now: float) -> str:
        if sid:
            return sid
        recent = [
            (float(entry.get("updated", 0.0)), key[1])
            for key, entry in self.sessions.items()
            if key[0] == source and key[2] is None
        ]
        if recent:
            newest, candidate = max(recent)
            if now - newest <= ADOPT_WINDOW:
                return candidate
        return "default"

    def _set(
        self,
        key: tuple[str, str, str | None],
        status: str,
        now: float,
        expires: float | None,
        event: str,
    ) -> None:
        self.sessions[key] = {
            "status": status,
            "source": key[0],
            "updated": now,
            "expires": expires,
            "event": event,
        }

    def _drop_session_tree(self, source: str, sid: str) -> None:
        """Remove exactly one main session and its own subagents — never the
        other sessions of the same source (the spec forbids that: one Codex
        task finishing must not touch a second, still-running Codex task)."""
        for key in list(self.sessions):
            if key[0] == source and key[1] == sid:
                del self.sessions[key]

    # -- event intake ----------------------------------------------------

    def apply(self, record: dict[str, Any], now: float | None = None) -> None:
        if not isinstance(record, dict):
            return
        now = float(record.get("recorded_at") or 0.0) if now is None else now
        if not now:
            now = time.time()
        if self._dedup(record.get("event_id")):
            return
        if record.get("event_id"):
            self.last_event_id = str(record["event_id"])
        source = str(record.get("source") or "unknown")
        event = str(record.get("event") or "")
        name = event.lower().replace("_", "")
        raw_sid = record.get("session_id")
        sid = self._resolve_sid(source, raw_sid, now)
        if (
            not raw_sid
            and name in _TERMINAL_EVENTS
            and self._live_main_sessions(source, now) > 1
        ):
            # Guessing which session a Stop belongs to is worse than dropping
            # it. Measured: an id-less Stop was routed to whichever session was
            # touched last, which ended a *different* project's marquee and
            # left the real one running until its lease expired. With one live
            # session the guess is safe; with several it is a coin flip, and
            # the lease is the correct backstop.
            self.dropped_ambiguous += 1
            return
        agent = record.get("agent_id") or None
        main = (source, sid, None)
        # Tool events fired by a subagent target that subagent's own entry so
        # a subagent finishing can never overwrite the main session.
        target = (source, sid, str(agent)) if agent else main

        if name == "sessionstart":
            # Open is not busy: merely switching to the chat tab must not
            # start the marquee. Register so id-less events can adopt it.
            if main not in self.sessions:
                self._set(main, "idle", now, None, event)
            else:
                self.sessions[main]["updated"] = now
        elif name == "userpromptsubmit":
            self._set(main, "working", now, now + WORKING_LEASE, event)
        elif name in _ACTIVITY_EVENTS or name == "subagentstart":
            # PostToolUseFailure included: a non-zero exit code is routine
            # mid-work (grep finding nothing exits 1) — the agent is still going.
            self._set(target, "working", now, now + WORKING_LEASE, event)
        elif name in {"permissionrequest", "elicitation"}:
            self._set(target, "approval", now, None, event)
        elif name == "permissiondenied":
            entry = self.sessions.get(target)
            if entry is not None and entry.get("status") == "approval":
                # The user said no to one tool; the turn itself continues.
                self._set(target, "working", now, now + WORKING_LEASE, event)
        elif name == "stop":
            self._end_main(main, "done", now, now + DONE_SECONDS, event)
        elif name == "stopfailure":
            self._end_main(main, "error", now, now + ERROR_SECONDS, event)
        elif name == "subagentstop":
            # Ends exactly one subagent. Never marks the main session done.
            if agent:
                self.sessions.pop(target, None)
        elif name == "taskcompleted":
            # A sub-task finishing must never light the session-level green.
            entry = self.sessions.get(target)
            if entry is not None and entry.get("status") == "working":
                entry["updated"] = now
                entry["expires"] = now + WORKING_LEASE
        elif name == "sessionend":
            self._drop_session_tree(source, sid)
            self._drop_stale_siblings(source, sid, now)
        elif name == "notification":
            category = str(record.get("notification_type") or "").lower()
            if category in {"permission", "idle"}:
                self._set(target, "approval", now, None, event)
            elif category == "error":
                self._set(main, "error", now, now + ERROR_SECONDS, event)
            # Anything unclassified is the agent asking for attention — the
            # opposite of the agent being busy. Ignore it.
        else:
            # Unknown event names count as activity from that session.
            self._set(target, "working", now, now + WORKING_LEASE, event)

    def _end_main(
        self,
        main: tuple[str, str, str | None],
        status: str,
        now: float,
        expires: float,
        event: str,
    ) -> None:
        self._set(main, status, now, expires, event)
        # The turn is over, so this session's own subagents are too.
        for key in list(self.sessions):
            if key[0] == main[0] and key[1] == main[1] and key[2] is not None:
                del self.sessions[key]
        self._drop_stale_siblings(main[0], main[1], now)

    def _drop_stale_siblings(self, source: str, sid: str, now: float) -> None:
        """Stand down long-inactive working siblings when a session finishes.

        This is the narrow fix for the split-session-id quirk (see
        STRANDED_AFTER): only sessions of the SAME source, only status
        "working", and only after STRANDED_AFTER of silence. It deliberately
        does NOT clear every working sibling — two genuinely concurrent tasks
        of one source must never end each other."""
        for key, entry in self.sessions.items():
            if key[0] != source or key[1] == sid or key[2] is not None:
                continue
            if entry.get("status") != "working":
                continue
            if now - float(entry.get("updated", now)) >= STRANDED_AFTER:
                entry["status"] = "idle"
                entry["expires"] = None

    # -- time & composition ---------------------------------------------

    def tick(self, now: float) -> None:
        """Expire timed statuses. Done/error decay to idle so the global
        state is recomputed from surviving sessions — never an unconditional
        fall to baseline."""
        for key, entry in list(self.sessions.items()):
            expires = entry.get("expires")
            if expires is not None and float(expires) <= now:
                entry["status"] = "idle"
                entry["expires"] = None
            if now - float(entry.get("updated", now)) > IDLE_PRUNE_AFTER:
                del self.sessions[key]

    def effect(self, now: float) -> str:
        statuses: set[str] = set()
        working_sources: set[str] = set()
        for entry in self.sessions.values():
            status = str(entry.get("status", "idle"))
            expires = entry.get("expires")
            if expires is not None and float(expires) <= now:
                continue
            statuses.add(status)
            if status == "working":
                working_sources.add(str(entry.get("source", "")))
        for candidate in _STATUS_PRIORITY:
            if candidate in statuses:
                return candidate
        if {"codex", "claude"} <= working_sources:
            return "working-both"
        if "codex" in working_sources:
            return "working-codex"
        if "claude" in working_sources:
            return "working-claude"
        return "baseline"

    def has_active_tasks(self, now: float) -> bool:
        return self.effect(now) != "baseline"

    def snapshot(self, now: float) -> dict[str, Any]:
        sessions = []
        for (source, sid, agent), entry in sorted(self.sessions.items()):
            expires = entry.get("expires")
            sessions.append(
                {
                    "source": source,
                    "session": sid,
                    "agent": agent,
                    "status": entry.get("status"),
                    "event": entry.get("event"),
                    "updated_ago": round(now - float(entry.get("updated", now)), 1),
                    "lease_left": (
                        round(float(expires) - now, 1) if expires is not None else None
                    ),
                }
            )
        return {
            "effect": self.effect(now),
            "last_event_id": self.last_event_id,
            "dropped_ambiguous": self.dropped_ambiguous,
            "sessions": sessions,
        }


def packet_for(effect: str, now: float) -> tuple[str, float]:
    """Composed effect -> (packet name, poll delay)."""
    if effect == "approval":
        return "permission", 0.25
    if effect == "error":
        # ~120 ms hard blink between red and off.
        return ("error" if int(now / 0.12) % 2 == 0 else "off"), 0.04
    if effect == "done":
        return "success", 0.25
    if effect == "working-codex":
        return "codex_working", 0.25
    if effect == "working-claude":
        return "claude_working", 0.25
    if effect == "working-both":
        # Blue and orange alternate on the BOTH_ALTERNATE_SECONDS cadence.
        half = int(now / BOTH_ALTERNATE_SECONDS) % 2
        return ("codex_working" if half == 0 else "claude_working"), 0.20
    return "baseline", 0.25


class Renderer:
    """Owns the HID writes. Resends deliberately on every effect change, so
    a green/red overlay expiring re-emits the underlying working packet even
    if it was the last thing on the wire before the overlay started."""

    def __init__(self, send: Callable[[str], None], log: Callable[[str], None]) -> None:
        self._send = send
        self._log = log
        self._last_effect: str | None = None
        self._last_packet: str | None = None

    def render(self, effect: str, now: float) -> float:
        packet, delay = packet_for(effect, now)
        if effect != self._last_effect:
            self._last_packet = None  # force one write per transition
            self._last_effect = effect
        if packet != self._last_packet:
            try:
                self._send(packet)
                self._last_packet = packet
                if packet not in {"error", "off"}:  # keep the blink out of the log
                    self._log(f"effect={effect} -> sent {packet.upper()}")
            except Exception as exc:
                self._log(f"effect={effect} -> HID write FAILED: {exc!r}")
        return delay


def parse_event_lines(chunk: str, log: Callable[[str], None]) -> list[dict[str, Any]]:
    """Parse complete JSONL lines; a broken line is logged and skipped."""
    records: list[dict[str, Any]] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            log(f"skipping corrupt event line ({len(line)} bytes)")
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            log("skipping non-object event line")
    return records


class EventTail:
    """Incremental reader for events.jsonl that survives partial last lines."""

    def __init__(self, path: Path, log: Callable[[str], None]) -> None:
        self.path = path
        self.offset = 0
        self._log = log
        self._pending = ""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            # In-place truncation (compaction) — start over.
            self.offset = 0
            self._pending = ""
        if size == self.offset:
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except OSError as exc:
            self._log(f"event log read failed: {exc!r}")
            return []
        data = self._pending + chunk
        # Writers flush whole lines, but the reader can still catch a line
        # mid-flight; keep the unterminated remainder for the next pass.
        complete, sep, remainder = data.rpartition("\n")
        self._pending = remainder if sep else data
        if not sep:
            return []
        return parse_event_lines(complete, self._log)


def _service_log(message: str) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if SERVICE_LOG.exists() and SERVICE_LOG.stat().st_size > 512_000:
            SERVICE_LOG.replace(SERVICE_LOG.with_suffix(".old.log"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with SERVICE_LOG.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _write_snapshot(machine: StateMachine, now: float, started_at: float) -> None:
    payload = {
        "pid": os.getpid(),
        "started_at": started_at,
        "written_at": now,
        **machine.snapshot(now),
    }
    try:
        # The service runs outside every sandbox and is this file's only
        # writer, so an atomic replace is safe here (unlike the old shared
        # state.json, which sandboxes turned into private copies).
        tmp = SNAPSHOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SNAPSHOT_FILE)
    except OSError:
        pass


def _maybe_compact(machine: StateMachine, now: float) -> None:
    """Truncate the event log in place when it is huge and nothing is live.

    Never rename or recreate events.jsonl: replacing the file is exactly the
    operation that gave each sandbox a private copy-on-write view. Truncating
    the same file keeps the identity hooks are appending to.
    """
    try:
        if EVENTS_FILE.stat().st_size < EVENTS_COMPACT_BYTES:
            return
    except OSError:
        return
    if machine.has_active_tasks(now):
        return
    try:
        with open(EVENTS_FILE, "r+", encoding="utf-8") as stream:
            stream.truncate(0)
        _service_log("compacted events.jsonl in place (no active tasks)")
    except OSError as exc:
        _service_log(f"compaction failed: {exc!r}")


def replay_events(machine: StateMachine, tail: EventTail) -> int:
    """Feed the whole existing log through the machine so a restarted
    service recovers every session whose lease has not yet expired."""
    count = 0
    while True:
        records = tail.read_new()
        if not records:
            break
        for record in records:
            machine.apply(record)
            count += 1
    return count


# --- Windows plumbing (mutex, stop event) --------------------------------

def _win():
    import ctypes

    return ctypes, ctypes.WinDLL("kernel32", use_last_error=True)


def acquire_single_instance():
    """Hold the service mutex; None means another service already runs."""
    ctypes, kernel32 = _win()
    ERROR_ALREADY_EXISTS = 183
    handle = kernel32.CreateMutexW(None, True, SERVICE_MUTEX)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return (kernel32, handle)


def service_mutex_held() -> bool:
    ctypes, kernel32 = _win()
    SYNCHRONIZE = 0x00100000
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, SERVICE_MUTEX)
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


class StopSignal:
    """Manual-reset named event polled by the service loop."""

    def __init__(self) -> None:
        ctypes, kernel32 = _win()
        self._kernel32 = kernel32
        self._handle = kernel32.CreateEventW(None, True, False, SERVICE_STOP_EVENT)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        # A stale signal from a previous stop must not kill the new service.
        kernel32.ResetEvent(self._handle)

    def is_set(self) -> bool:
        WAIT_OBJECT_0 = 0
        return self._kernel32.WaitForSingleObject(self._handle, 0) == WAIT_OBJECT_0

    def close(self) -> None:
        self._kernel32.CloseHandle(self._handle)


def request_stop() -> bool:
    """Fire the stop event. False when no service has the event open."""
    ctypes, kernel32 = _win()
    EVENT_MODIFY_STATE = 0x0002
    kernel32.OpenEventW.restype = ctypes.c_void_p
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SERVICE_STOP_EVENT)
    if not handle:
        return False
    handle = ctypes.c_void_p(handle)
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


# --- the service loop ----------------------------------------------------

def run_service(
    verbose: bool = False,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """Foreground service loop. The only caller of send_packet anywhere."""
    import keyboard_lights as kl

    def report(message: str) -> None:
        _service_log(message)
        if verbose:
            print(f"[service] {message}", flush=True)

    instance = acquire_single_instance()
    if instance is None:
        report("another light service already holds the mutex; exiting")
        return 0
    kernel32, mutex_handle = instance

    stop_signal: StopSignal | None = None
    if stop_check is None:
        stop_signal = StopSignal()
        stop_check = stop_signal.is_set

    def send(packet_name: str) -> None:
        kl.send_packet(kl.DEVICE.packets[packet_name])

    machine = StateMachine()
    tail = EventTail(EVENTS_FILE, report)
    renderer = Renderer(send, report)
    started_at = time.time()
    report(f"service started pid={os.getpid()} device={kl.DEVICE.key}")

    replayed = replay_events(machine, tail)
    now = time.time()
    machine.tick(now)
    report(f"replayed {replayed} events; effect={machine.effect(now)}")
    _maybe_compact(machine, now)

    last_snapshot = 0.0
    try:
        while not stop_check():
            now = time.time()
            for record in tail.read_new():
                machine.apply(record, now=now)
            machine.tick(now)
            effect = machine.effect(now)
            delay = renderer.render(effect, now)
            if now - last_snapshot >= 2.0:
                _write_snapshot(machine, now, started_at)
                last_snapshot = now
            time.sleep(delay)
    except Exception as exc:
        report(f"service crashed: {exc!r}")
        raise
    finally:
        now = time.time()
        machine.tick(now)
        if not machine.has_active_tasks(now):
            # Only restore the teal idle light when nothing is running; a
            # stopping service must not repaint over a still-working agent.
            try:
                send("baseline")
                report("service exit: restored baseline (no active tasks)")
            except Exception as exc:
                report(f"service exit: could not restore baseline: {exc!r}")
        else:
            report("service exit: active tasks remain, leaving lights as-is")
        _write_snapshot(machine, time.time(), started_at)
        if stop_signal is not None:
            stop_signal.close()
        kernel32.ReleaseMutex(mutex_handle)
        kernel32.CloseHandle(mutex_handle)
    report("service stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_service(verbose="--verbose" in sys.argv))
