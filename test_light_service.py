"""Unit tests for the unified light service (handoff spec, automated part).

Pure state-machine tests: no HID, no real service, temp dirs only.
Run: python -m unittest test_light_service -v
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
import uuid

import light_backends
import light_client
import light_service
from light_service import (
    DONE_SECONDS,
    ERROR_SECONDS,
    STRANDED_AFTER,
    WORKING_LEASE,
    EventTail,
    _TERMINAL_EVENTS,
    Renderer,
    StateMachine,
    packet_for,
    parse_event_lines,
    replay_events,
)

T0 = 1_788_000_000.0  # arbitrary fixed epoch


def ev(source, event, sid=None, t=T0, agent=None, event_id=None, ntype=None):
    record = {
        "schema": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "recorded_at": t,
        "source": source,
        "event": event,
    }
    if sid:
        record["session_id"] = sid
    if agent:
        record["agent_id"] = agent
    if ntype:
        record["notification_type"] = ntype
    return record


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.m = StateMachine()

    def effect_at(self, t):
        self.m.tick(t)
        return self.m.effect(t)

    # 1. Codex A/B 同时工作，A Stop 后 B 仍为蓝色
    def test_codex_stop_keeps_other_codex_session(self):
        self.m.apply(ev("codex", "UserPromptSubmit", "A", T0))
        self.m.apply(ev("codex", "UserPromptSubmit", "B", T0 + 1))
        self.assertEqual(self.effect_at(T0 + 2), "working-codex")
        self.m.apply(ev("codex", "Stop", "A", T0 + 5))
        self.assertEqual(self.effect_at(T0 + 6), "done")  # 绿色优先显示
        self.assertEqual(self.effect_at(T0 + 5 + DONE_SECONDS + 0.1), "working-codex")

    # 2. Claude A/B 同时工作，A SessionEnd 后 B 仍为橙色
    def test_claude_sessionend_keeps_other_claude_session(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "A", T0))
        self.m.apply(ev("claude", "UserPromptSubmit", "B", T0 + 1))
        self.m.apply(ev("claude", "SessionEnd", "A", T0 + 5))
        self.assertEqual(self.effect_at(T0 + 6), "working-claude")

    # 3. 双方同时工作时按 2 秒交替蓝橙
    def test_both_working_alternates_every_two_seconds(self):
        self.m.apply(ev("codex", "PreToolUse", "cx", T0))
        self.m.apply(ev("claude", "PreToolUse", "cl", T0))
        self.assertEqual(self.effect_at(T0 + 1), "working-both")
        step = light_service.BOTH_ALTERNATE_SECONDS
        base = step * 100  # lands on an even multiple, i.e. the codex half
        self.assertEqual(packet_for("working-both", base)[0], "codex_working")
        self.assertEqual(packet_for("working-both", base + step)[0], "claude_working")
        self.assertEqual(packet_for("working-both", base + 2 * step)[0], "codex_working")
        # still the same colour just before the cadence flips
        self.assertEqual(packet_for("working-both", base + step * 0.9)[0], "codex_working")

    # 4/5. 一方 Stop 绿灯 10 秒后自动恢复另一方的工作灯，且必须重发数据包
    def _stop_recovers_survivor(self, stopper, survivor, survivor_packet):
        self.m.apply(ev(stopper, "UserPromptSubmit", "a", T0))
        self.m.apply(ev(survivor, "UserPromptSubmit", "b", T0))
        self.m.apply(ev(stopper, "Stop", "a", T0 + 5))

        sent = []
        renderer = Renderer(sent.append, lambda _m: None)
        renderer.render(self.effect_at(T0 + 6), T0 + 6)
        self.assertEqual(sent[-1], "success")
        after = T0 + 5 + DONE_SECONDS + 0.1
        renderer.render(self.effect_at(after), after)
        # 无需任何新 Hook 事件，恢复包已重新发送
        self.assertEqual(sent[-1], survivor_packet)

    def test_claude_stop_then_codex_recovers_blue(self):
        self._stop_recovers_survivor("claude", "codex", "codex_working")

    def test_codex_stop_then_claude_recovers_orange(self):
        self._stop_recovers_survivor("codex", "claude", "claude_working")

    # 6. SubagentStop 不触发主会话绿色，也不停主会话跑马灯
    def test_subagent_stop_leaves_main_session_alone(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "S", T0))
        self.m.apply(ev("claude", "SubagentStart", "S", T0 + 1, agent="sub1"))
        self.m.apply(ev("claude", "SubagentStop", "S", T0 + 2, agent="sub1"))
        self.assertEqual(self.effect_at(T0 + 3), "working-claude")

    # 7. TaskCompleted 不结束仍在运行的主会话
    def test_task_completed_does_not_finish_session(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "S", T0))
        self.m.apply(ev("claude", "TaskCompleted", "S", T0 + 1))
        self.assertEqual(self.effect_at(T0 + 2), "working-claude")
        # 单独一个 TaskCompleted 也绝不能点绿灯
        fresh = StateMachine()
        fresh.apply(ev("claude", "TaskCompleted", "X", T0))
        fresh.tick(T0 + 1)
        self.assertNotEqual(fresh.effect(T0 + 1), "done")

    # 8. PermissionRequest 紫色；批准/拒绝/后续工具事件恢复工作灯
    def test_permission_flow(self):
        self.m.apply(ev("codex", "UserPromptSubmit", "S", T0))
        self.m.apply(ev("codex", "PermissionRequest", "S", T0 + 1))
        self.assertEqual(self.effect_at(T0 + 2), "approval")
        self.m.apply(ev("codex", "PostToolUse", "S", T0 + 3))
        self.assertEqual(self.effect_at(T0 + 4), "working-codex")
        self.m.apply(ev("codex", "PermissionRequest", "S", T0 + 5))
        self.m.apply(ev("codex", "PermissionDenied", "S", T0 + 6))
        self.assertEqual(self.effect_at(T0 + 7), "working-codex")

    # 9. StopFailure 红色急闪 10 秒，之后恢复其他任务灯效
    def test_stop_failure_red_then_recovers(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "bad", T0))
        self.m.apply(ev("codex", "UserPromptSubmit", "ok", T0))
        self.m.apply(ev("claude", "StopFailure", "bad", T0 + 5))
        self.assertEqual(self.effect_at(T0 + 6), "error")
        # 红色是 error/off 交替
        names = {packet_for("error", T0 + 6)[0], packet_for("error", T0 + 6.12)[0]}
        self.assertEqual(names, {"error", "off"})
        self.assertEqual(self.effect_at(T0 + 5 + ERROR_SECONDS + 0.1), "working-codex")

    # 10. 重复 event_id 只处理一次
    def test_duplicate_event_id_processed_once(self):
        start = ev("claude", "UserPromptSubmit", "S", T0, event_id="dup-1")
        self.m.apply(start)
        self.m.apply(ev("claude", "Stop", "S", T0 + 5))
        self.m.apply(dict(start))  # 重放同一事件
        self.assertEqual(self.effect_at(T0 + 6), "done")  # 没有被拉回 working

    # 租约：无事件续期的 working 到期后不再点灯
    def test_working_lease_expires(self):
        self.m.apply(ev("codex", "UserPromptSubmit", "S", T0))
        self.assertEqual(self.effect_at(T0 + WORKING_LEASE - 1), "working-codex")
        self.assertEqual(self.effect_at(T0 + WORKING_LEASE + 1), "baseline")

    # 无 session_id 的事件挂到同来源最近的活动会话
    def test_idless_event_adopts_newest_session(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "S", T0))
        self.m.apply(ev("claude", "Stop", None, T0 + 5))
        self.assertEqual(self.effect_at(T0 + 5 + DONE_SECONDS + 0.1), "baseline")

    # SessionStart 只注册不点灯
    def test_session_start_stays_idle(self):
        self.m.apply(ev("claude", "SessionStart", "S", T0))
        self.assertEqual(self.effect_at(T0 + 1), "baseline")

    # 桌面版把一个对话拆成两个 id：工作事件走 A、Stop 走 B。
    # B 的 Stop 必须把沉默已久的 A 一起收掉，否则 A 的租约撑着橙灯半小时。
    def test_split_session_id_stop_clears_stale_twin(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "work-id", T0))
        self.m.apply(ev("claude", "PreToolUse", "work-id", T0 + 10))
        stop_at = T0 + 10 + STRANDED_AFTER + 1
        self.m.apply(ev("claude", "Stop", "stop-id", stop_at))
        self.assertEqual(self.effect_at(stop_at + 1), "done")  # 绿灯照常
        self.assertEqual(self.effect_at(stop_at + DONE_SECONDS + 0.1), "baseline")

    # 但活跃的并发会话（刚有活动）绝不能被别的会话的 Stop 收掉
    def test_active_sibling_survives_other_stop(self):
        self.m.apply(ev("claude", "PreToolUse", "busy", T0))
        self.m.apply(ev("claude", "Stop", "other", T0 + 30))  # busy 才安静 30s
        self.assertEqual(self.effect_at(T0 + 30 + DONE_SECONDS + 0.1), "working-claude")

    # 被误收的会话下一个事件一来就复活
    def test_stood_down_session_revives_on_next_event(self):
        self.m.apply(ev("claude", "PreToolUse", "slow", T0))
        stop_at = T0 + STRANDED_AFTER + 1
        self.m.apply(ev("claude", "Stop", "other", stop_at))
        self.assertEqual(self.effect_at(stop_at + DONE_SECONDS + 0.1), "baseline")
        self.m.apply(ev("claude", "PostToolUse", "slow", stop_at + 20))
        self.assertEqual(self.effect_at(stop_at + 21), "working-claude")

    # SessionEnd 同样清理沉默的孪生会话
    def test_sessionend_clears_stale_twin(self):
        self.m.apply(ev("claude", "UserPromptSubmit", "work-id", T0))
        end_at = T0 + STRANDED_AFTER + 1
        self.m.apply(ev("claude", "SessionEnd", "stop-id", end_at))
        self.assertEqual(self.effect_at(end_at + 1), "baseline")

    # 普通 Notification 不点灯；permission/idle 类转紫；error 类转红
    def test_notification_categories(self):
        self.m.apply(ev("claude", "Notification", "S", T0))
        self.assertEqual(self.effect_at(T0 + 1), "baseline")
        self.m.apply(ev("claude", "Notification", "S", T0 + 2, ntype="permission"))
        self.assertEqual(self.effect_at(T0 + 3), "approval")


class EventLogTest(unittest.TestCase):
    # 11. 损坏 JSON 行被记录并跳过
    def test_corrupt_lines_skipped(self):
        logged = []
        records = parse_event_lines(
            '{"event": "Stop", "source": "codex"}\n'
            "{broken json!!\n"
            "[1, 2, 3]\n"
            '{"event": "PreToolUse", "source": "claude"}',
            logged.append,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(len(logged), 2)

    # 12. 服务重启后重放事件日志恢复未过期会话
    def test_replay_restores_live_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            fresh = ev("codex", "UserPromptSubmit", "live", T0)
            stale = ev("claude", "UserPromptSubmit", "old", T0 - WORKING_LEASE - 60)
            with open(path, "w", encoding="utf-8") as f:
                import json

                for record in (stale, fresh):
                    f.write(json.dumps(record) + "\n")
            machine = StateMachine()
            replay_events(machine, EventTail(path, lambda _m: None))
            machine.tick(T0 + 1)
            # 过期租约不复活，未过期会话恢复
            self.assertEqual(machine.effect(T0 + 1), "working-codex")

    def test_tail_handles_partial_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            tail = EventTail(path, lambda _m: None)
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"event": "Pre')
                f.flush()
                self.assertEqual(tail.read_new(), [])  # 半行不处理
                f.write('ToolUse", "source": "codex"}\n')
            records = tail.read_new()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["event"], "PreToolUse")


@unittest.skipUnless(os.name == "nt", "Windows named mutex")
class SingleInstanceTest(unittest.TestCase):
    # 13. 两个并发启动请求只留下一个实例
    def test_second_instance_refused(self):
        original = light_service.SERVICE_MUTEX
        light_service.SERVICE_MUTEX = f"Local\\MTL-test-{uuid.uuid4()}"
        try:
            first = light_service.acquire_single_instance()
            self.assertIsNotNone(first)
            self.assertIsNone(light_service.acquire_single_instance())
            self.assertTrue(light_service.service_mutex_held())
            kernel32, handle = first
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
            self.assertFalse(light_service.service_mutex_held())
        finally:
            light_service.SERVICE_MUTEX = original


class ClientTest(unittest.TestCase):
    def test_notification_text_never_recorded(self):
        record = light_client.build_event(
            "claude",
            "Notification",
            {"session_id": "S", "message": "Claude needs your permission to use Bash"},
        )
        self.assertEqual(record["notification_type"], "permission")
        self.assertNotIn("message", record)
        self.assertNotIn("permission to use Bash", str(record))

    def test_cwd_fallback_is_hashed(self):
        record = light_client.build_event(
            "codex", "PreToolUse", {"cwd": r"C:\secret\project"}
        )
        self.assertTrue(record["session_id"].startswith("cwd-"))
        self.assertNotIn("secret", record["session_id"])

    def test_append_and_replay_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            record = light_client.build_event(
                "claude", "UserPromptSubmit", {"session_id": "rt"}
            )
            light_client.append_event(record, events_file=path)
            machine = StateMachine()
            replay_events(machine, EventTail(path, lambda _m: None))
            now = record["recorded_at"] + 1
            machine.tick(now)
            self.assertEqual(machine.effect(now), "working-claude")


class StdinDecodingTest(unittest.TestCase):
    """Regression: hook payloads are UTF-8, not the console codepage.

    Windows gave a piped stdin the gbk codepage, so any payload naming a
    non-ASCII path decoded into mojibake containing a stray backslash and
    failed to parse. The event then reached the service with no session_id.
    """

    class _FakeStdin:
        def __init__(self, raw: bytes) -> None:
            self.buffer = io.BytesIO(raw)

        def isatty(self) -> bool:
            return False

    def _parse(self, payload: dict) -> dict:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        original, sys.stdin = sys.stdin, self._FakeStdin(raw)
        try:
            return light_client.read_hook_payload([])
        finally:
            sys.stdin = original

    def test_non_ascii_path_payload_keeps_session_id(self):
        payload = {
            "session_id": "S-1",
            "hook_event_name": "PreToolUse",
            "cwd": r"C:\Users\x\Documents\ChatGPT\键盘光效",
        }
        parsed = self._parse(payload)
        self.assertEqual(parsed.get("session_id"), "S-1")
        self.assertEqual(
            light_client.build_event("claude", "PreToolUse", parsed).get("session_id"),
            "S-1",
        )

    def test_plain_ascii_payload_still_parses(self):
        self.assertEqual(self._parse({"session_id": "S-2"}).get("session_id"), "S-2")


class AmbiguousTerminalEventTest(unittest.TestCase):
    """An id-less Stop must never be guessed onto one of several sessions."""

    def test_idless_stop_dropped_when_several_sessions_live(self):
        m = StateMachine()
        m.apply(ev("claude", "UserPromptSubmit", "proj-a", T0))
        m.apply(ev("claude", "UserPromptSubmit", "proj-b", T0 + 1))
        m.apply(ev("claude", "Stop", None, T0 + 2))
        self.assertEqual(m.dropped_ambiguous, 1)
        m.tick(T0 + 3)
        # Neither project was ended by the guess.
        self.assertEqual(m.effect(T0 + 3), "working-claude")
        statuses = {e["status"] for e in m.sessions.values()}
        self.assertEqual(statuses, {"working"})

    def test_idless_stop_still_applied_when_only_one_session(self):
        m = StateMachine()
        m.apply(ev("claude", "UserPromptSubmit", "solo", T0))
        m.apply(ev("claude", "Stop", None, T0 + 2))
        self.assertEqual(m.dropped_ambiguous, 0)
        m.tick(T0 + 3)
        self.assertEqual(m.effect(T0 + 3), "done")

    def test_terminal_set_covers_the_session_enders(self):
        self.assertEqual(_TERMINAL_EVENTS, {"stop", "stopfailure", "sessionend"})


class HidFailureReportingTest(unittest.TestCase):
    """A keyboard this build cannot drive must be loud, not silent."""

    def test_failures_are_counted_and_throttled(self):
        logged = []
        def always_fails(_packet):
            raise RuntimeError("device unavailable")
        r = Renderer(always_fails, logged.append)
        for i in range(12):
            r.render("working-codex", T0 + i)
        self.assertEqual(r.consecutive_failures, 12)
        self.assertIn("device unavailable", r.last_error or "")
        # logged at the 1st and 10th failure only -- not 12 times
        self.assertEqual(len(logged), 2)

    def test_recovery_is_reported_and_resets(self):
        logged = []
        state = {"fail": True}
        def flaky(_packet):
            if state["fail"]:
                raise RuntimeError("nope")
        r = Renderer(flaky, logged.append)
        r.render("working-codex", T0)
        self.assertEqual(r.consecutive_failures, 1)
        state["fail"] = False
        r.render("working-claude", T0 + 1)
        self.assertEqual(r.consecutive_failures, 0)
        self.assertIsNone(r.last_error)
        self.assertTrue(any("recovered" in m for m in logged))


class BackendTest(unittest.TestCase):
    """The semantic table must describe the real captured bytes, not a
    parallel invented vocabulary -- otherwise a second backend would render
    something subtly different from what this keyboard has always shown."""

    def test_semantics_match_the_captured_packets(self):
        import keyboard_lights as kl
        for name, (animation, rgb) in light_backends.EFFECT_SEMANTICS.items():
            packet = kl.DEVICE.packets[name]
            self.assertEqual(
                (packet[9], packet[10], packet[11]), rgb,
                f"{name}: semantic colour disagrees with the captured packet",
            )
            self.assertIn(animation, light_backends.ANIMATIONS)

    def test_every_required_effect_has_semantics(self):
        import keyboard_lights as kl
        self.assertEqual(
            set(light_backends.EFFECT_SEMANTICS), set(kl.REQUIRED_EFFECTS)
        )

    def test_captured_backend_is_preferred_when_present(self):
        class FakeKL:
            DEVICE = type("D", (), {"name": "x", "hid_id": "y", "packets": {}})()
            @staticmethod
            def device_present(): return True
            @staticmethod
            def send_packet(_p): pass
        backend, notes = light_backends.resolve_backend(FakeKL())
        self.assertEqual(backend.name, "captured")
        self.assertTrue(any("available" in n for n in notes))

    def test_no_backend_when_nothing_is_drivable(self):
        class FakeKL:
            DEVICE = type("D", (), {"name": "x", "hid_id": "y", "packets": {}})()
            @staticmethod
            def device_present(): return False
        backend, notes = light_backends.resolve_backend(FakeKL())
        self.assertIsNone(backend)
        self.assertTrue(any("not available" in n for n in notes))

    def test_captured_backend_sends_the_exact_bytes(self):
        sent = []
        class FakeKL:
            DEVICE = type("D", (), {"name": "x", "hid_id": "y",
                                    "packets": {"success": b"rest"}})()
            @staticmethod
            def device_present(): return True
            @staticmethod
            def send_packet(p): sent.append(p)
        light_backends.CapturedBackend(FakeKL()).send("success")
        self.assertEqual(sent, [b"rest"])


if __name__ == "__main__":
    unittest.main()
