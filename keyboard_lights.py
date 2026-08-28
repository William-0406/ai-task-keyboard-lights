"""K500-M81 task lighting controller for Codex and Claude hooks.

Only the verified 0x07 lighting command is emitted. No firmware, key-map, or
Hall-effect configuration commands are implemented in this program.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Iterator

_INLINE_STOP = threading.Event()


# Every keyboard's identity and its captured lighting packets live in
# devices/*.json, so adding a model is a data change, not a code change. The
# profile is chosen by matching its VID/PID against the HID devices actually
# present; KEYBOARD_LIGHTS_DEVICE or --device overrides that.
DEVICE_DIR = Path(__file__).resolve().parent / "devices"
DEVICE_ENV = "KEYBOARD_LIGHTS_DEVICE"
REQUIRED_EFFECTS = (
    "baseline",
    "codex_working",
    "claude_working",
    "success",
    "permission",
    "error",
    "off",
)


def _captured_packet(body: str, tail: str, report_length: int) -> bytes:
    """Rebuild a captured report: command body, driver padding, captured tail."""
    prefix = bytes.fromhex(body)
    suffix = bytes.fromhex(tail)
    padding = report_length - len(prefix) - len(suffix)
    if padding < 0:
        raise ValueError("Captured lighting packet is longer than the report")
    return prefix + bytes(padding) + suffix


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MachenikeTaskLights"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "keyboard-lights.log"
STATE_MUTEX = "Local\\MachenikeTaskLights-State-v1"
DAEMON_MUTEX = "Local\\MachenikeTaskLights-Daemon-v1"
# A live daemon refreshes its heartbeat every ~2s. If the mutex is held but no
# heartbeat has landed within this window, the holder is wedged (a synchronous
# HID WriteFile can block forever) and a new daemon takes over instead of
# exiting, which is what used to leave the lights permanently dead.
DAEMON_HEARTBEAT_INTERVAL = 2.0
DAEMON_STALE_AFTER = 8.0
# A "working" entry is a claim that an agent is busy *right now*. Real work
# keeps refreshing that claim -- every tool call fires PreToolUse and
# PostToolUse -- so the lease only has to outlive the gap between two events.
# Without a lease a single stray "working" pinned the marquee on until the
# 6-hour prune, which is what left the lights running while nothing was going on.
WORKING_TTL = 600.0
# Hooks do not always deliver their JSON payload. An event with no session id
# is attached to the most recent live session of the same source, provided that
# session was touched this recently.
SESSION_ADOPT_WINDOW = 30 * 60.0
# One conversation can report several session ids -- the desktop app has been
# observed firing PreToolUse under one id and PostToolUse under another, seconds
# apart. Only one of those ids gets the Stop, so keeping the others alive left
# the marquee running with nothing to run for. When any session of a source
# finishes, every other *working* session of that source is dropped with it.
# Pending approvals are never dropped: hiding a permission prompt is worse than
# a stale marquee. A genuinely concurrent agent revives on its next event.

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
WAIT_OBJECT_0 = 0x00000000
ERROR_ALREADY_EXISTS = 183


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID),
    wintypes.LPCWSTR,
    wintypes.HWND,
    wintypes.DWORD,
]
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(GUID),
    wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetAttributes.restype = wintypes.BOOLEAN
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
SYNCHRONIZE = 0x00100000
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]


def _log(message: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        pass


@contextlib.contextmanager
def _named_mutex(name: str, timeout_ms: int = 5000) -> Iterator[None]:
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = kernel32.WaitForSingleObject(handle, timeout_ms)
        if result != WAIT_OBJECT_0:
            raise TimeoutError(f"Timed out waiting for {name}")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
    finally:
        kernel32.CloseHandle(handle)


def _daemon_mutex_held() -> bool:
    """True when some process currently owns the daemon mutex."""
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, DAEMON_MUTEX)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def _enumerate_hid_paths() -> list[str]:
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if info == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    paths: list[str] = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(interface)
            ok = setupapi.SetupDiEnumDeviceInterfaces(
                info, None, ctypes.byref(guid), index, ctypes.byref(interface)
            )
            if not ok:
                break
            index += 1

            required = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info, ctypes.byref(interface), None, 0, ctypes.byref(required), None
            )
            detail = ctypes.create_string_buffer(required.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W is 8 bytes on 64-bit Windows,
            # but DevicePath still begins immediately after the 4-byte cbSize.
            ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            )
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                info,
                ctypes.byref(interface),
                detail,
                required.value,
                None,
                None,
            )
            if ok:
                paths.append(ctypes.wstring_at(ctypes.addressof(detail) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


class Device:
    """One keyboard's HID identity plus its captured lighting packets."""

    __slots__ = (
        "key", "name", "firmware", "vid", "pid",
        "marker", "report_length", "prefix", "packets",
    )

    def __init__(self, key: str, raw: dict[str, Any]) -> None:
        self.key = key
        self.name = str(raw.get("name") or key)
        self.firmware = str(raw.get("firmware") or "")
        self.vid = int(str(raw["vid"]), 16)
        self.pid = int(str(raw["pid"]), 16)
        self.marker = str(raw.get("interface_marker") or "").lower()
        self.report_length = int(raw.get("report_length", 64))
        self.prefix = bytes.fromhex(raw["lighting_prefix"])
        if not self.prefix:
            raise ValueError(f"{key}: lighting_prefix must not be empty")
        self.packets: dict[str, bytes] = {}
        for effect in REQUIRED_EFFECTS:
            body, tail = raw["packets"][effect]
            packet = _captured_packet(body, tail, self.report_length)
            # lighting_prefix IS the safety gate. Checking every packet against
            # it here means a device profile cannot smuggle a firmware or
            # key-map command past send_packet by calling it a lighting effect.
            if not packet.startswith(self.prefix):
                raise ValueError(
                    f"{key}: packet '{effect}' does not start with lighting_prefix"
                )
            self.packets[effect] = packet

    @property
    def hid_id(self) -> str:
        return f"vid_{self.vid:04x}&pid_{self.pid:04x}"

    def is_present(self, paths: list[str]) -> bool:
        return any(self.hid_id in path.lower() for path in paths)


def load_devices() -> dict[str, Device]:
    """Read every usable profile in devices/. A broken file is skipped, not fatal."""
    found: dict[str, Device] = {}
    for path in sorted(DEVICE_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            found[path.stem] = Device(path.stem, raw)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _log(f"device profile {path.name} ignored: {exc!r}")
    if not found:
        raise RuntimeError(f"devices/ 里没有可用的设备配置：{DEVICE_DIR}")
    return found


def select_device(preferred: str | None = None) -> Device:
    devices = load_devices()
    if preferred:
        if preferred not in devices:
            raise SystemExit(
                f"未知设备 '{preferred}'。可用：{', '.join(sorted(devices))}"
            )
        return devices[preferred]
    if len(devices) > 1:
        try:
            paths = _enumerate_hid_paths()
        except OSError:
            paths = []
        for device in devices.values():
            if device.is_present(paths):
                return device
    return next(iter(devices.values()))


_PACKET_NAMES: dict[bytes, str] = {}


def use_device(device: Device) -> None:
    """Point the module's packet globals at one profile."""
    global DEVICE, VID, PID, INTERFACE_MARKER, REPORT_LENGTH, LIGHTING_PREFIX
    global BASELINE_PACKET, CODEX_WORKING_PACKET, CLAUDE_WORKING_PACKET
    global SUCCESS_PACKET, PERMISSION_PACKET, ERROR_PACKET, OFF_PACKET
    DEVICE = device
    VID = device.vid
    PID = device.pid
    INTERFACE_MARKER = device.marker
    REPORT_LENGTH = device.report_length
    LIGHTING_PREFIX = device.prefix
    BASELINE_PACKET = device.packets["baseline"]
    CODEX_WORKING_PACKET = device.packets["codex_working"]
    CLAUDE_WORKING_PACKET = device.packets["claude_working"]
    SUCCESS_PACKET = device.packets["success"]
    PERMISSION_PACKET = device.packets["permission"]
    ERROR_PACKET = device.packets["error"]
    OFF_PACKET = device.packets["off"]
    _PACKET_NAMES.clear()


use_device(select_device(os.environ.get(DEVICE_ENV) or None))


def _open_keyboard() -> tuple[int, str]:
    wanted = f"vid_{VID:04x}&pid_{PID:04x}"
    errors: list[str] = []
    for path in _enumerate_hid_paths():
        lower = path.lower()
        if wanted not in lower or INTERFACE_MARKER not in lower:
            continue
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            errors.append(f"{path}: winerror={ctypes.get_last_error()}")
            continue
        attributes = HIDD_ATTRIBUTES()
        attributes.Size = ctypes.sizeof(attributes)
        if hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
            if attributes.VendorID == VID and attributes.ProductID == PID:
                return int(handle), path
        kernel32.CloseHandle(handle)
    detail = "; ".join(errors) if errors else "matching MI_02 interface not found"
    raise RuntimeError(f"{DEVICE.name} is unavailable: {detail}")


def send_packet(packet: bytes) -> None:
    if len(packet) != REPORT_LENGTH:
        raise ValueError(f"Expected {REPORT_LENGTH} bytes, got {len(packet)}")
    # Safety gate: this program can only send the verified lighting selector.
    if not packet.startswith(LIGHTING_PREFIX):
        raise ValueError("Refusing to send a non-lighting HID command")
    handle, _ = _open_keyboard()
    try:
        buffer = ctypes.create_string_buffer(packet)
        written = wintypes.DWORD()
        ok = kernel32.WriteFile(handle, buffer, len(packet), ctypes.byref(written), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(packet):
            raise OSError(f"Short HID write: {written.value}/{len(packet)}")
    finally:
        kernel32.CloseHandle(handle)


def _default_state() -> dict[str, Any]:
    return {"version": 2, "generation": 0, "sessions": {}}


def _load_state() -> dict[str, Any]:
    """Read the shared state, retrying through transient read failures.

    A failed read must never look like an empty state. Callers write back what
    they get, so handing them a blank slate silently deletes every other
    agent's session -- with Codex and Claude both firing hooks, Windows returns
    sharing violations while the other process is mid-os.replace, and the two
    agents were erasing each other several times a minute. When the read really
    cannot be completed the state is flagged instead, and _save_state refuses
    to persist it, so at worst this one event is dropped.
    """
    problem: Exception | None = None
    for attempt in range(5):
        try:
            raw = STATE_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _default_state()  # genuinely nothing yet
        except OSError as exc:
            problem = exc
            time.sleep(0.02 * (attempt + 1))
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            problem = exc
            time.sleep(0.02 * (attempt + 1))
            continue
        if isinstance(value, dict) and value.get("version") == 2:
            return value
        return _default_state()
    _log(f"state unreadable after retries, refusing to overwrite: {problem!r}")
    unusable = _default_state()
    unusable["_unreadable"] = True
    return unusable


def _save_state(state: dict[str, Any]) -> None:
    if state.get("_unreadable"):
        return  # never let a failed read overwrite everyone else's sessions
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def _read_hook_payload(extra_args: list[str]) -> dict[str, Any]:
    candidates = [arg for arg in extra_args if arg.lstrip().startswith("{")]
    if not sys.stdin.isatty():
        try:
            incoming = sys.stdin.read()
            if incoming.strip():
                candidates.append(incoming)
        except OSError:
            pass
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {}


def _drop_stranded(
    sessions: dict[str, Any], source: str, session: str
) -> None:
    """Remove abandoned keys of one source when a session of it finishes."""
    for key in list(sessions):
        if key == session:
            continue
        item = sessions[key]
        if item.get("source") != source:
            continue
        if item.get("status") == "working":
            sessions.pop(key, None)


def _resolve_session(
    source: str,
    payload: dict[str, Any],
    sessions: dict[str, Any],
    now: float,
) -> str:
    """Map one hook event onto a session key.

    Events whose payload never arrived used to pile into a shared
    "<source>:default" bucket that nothing could ever stop: the real session's
    Stop cleared its own key, the orphan stayed "working" forever, and the
    marquee came back 10s after the done flash. Attaching an id-less event to
    the newest live session of the same source keeps it reachable by that
    session's own Stop.
    """
    for key in ("session_id", "thread_id", "conversation_id", "task_id"):
        value = payload.get(key)
        if value:
            return f"{source}:{value}"
    cwd = payload.get("cwd")
    if cwd:
        return f"{source}:{cwd}"
    recent = [
        (float(item.get("updated", 0) or 0), key)
        for key, item in sessions.items()
        if item.get("source") == source
    ]
    if recent:
        newest, key = max(recent)
        if now - newest <= SESSION_ADOPT_WINDOW:
            return key
    return f"{source}:default"


def _normalize_event(event: str, payload: dict[str, Any]) -> tuple[str, float | None]:
    name = event.lower().replace("_", "")
    if name in {"permissionrequest", "elicitation"}:
        return "approval", None
    if name == "sessionstart":
        # A session that just opened is waiting for a prompt, not working.
        # Counting it as work is why merely switching to the chat tab started
        # the marquee. The caller still spawns the daemon on this event.
        return "ignore", None
    if name == "notification":
        notification = str(payload.get("notification_type", "")).lower()
        if "permission" in notification or "idle" in notification:
            return "approval", None
        if "error" in notification or "fail" in notification:
            return "error", time.time() + 10.0
        # An unclassified notification is the agent asking for attention, which
        # is the opposite of the agent being busy.
        return "ignore", None
    if name in {"stop", "subagentstop"}:
        # A Stop with no assistant message is the user hitting stop. That is
        # their decision, not a failure, so it gets the same green finish as a
        # turn that ran to completion.
        return "done", time.time() + 10.0
    if name == "taskcompleted":
        return "done", time.time() + 10.0
    # NB: PostToolUseFailure is deliberately absent. A non-zero exit code is
    # routine mid-work -- grep finding nothing exits 1 -- and turning the
    # keyboard red for it meant red fired every few minutes and stopped
    # meaning anything. It falls through to "working": the agent is still going.
    if name in {
        "error",
        "failed",
        "connectionfailed",
        "answerfailed",
        "stopfailure",
    }:
        return "error", time.time() + 10.0
    if name in {"sessionend"}:
        return "remove", None
    return "working", time.time() + WORKING_TTL


def record_event(source: str, event: str, payload: dict[str, Any]) -> None:
    status, expires = _normalize_event(event, payload)
    now = time.time()
    with _named_mutex(STATE_MUTEX):
        state = _load_state()
        sessions = state.setdefault("sessions", {})
        session = _resolve_session(source, payload, sessions, now)
        # One line per incoming hook. The effect log alone cannot tell a
        # mis-mapped event from a mis-rendered light, which is what made the
        # stranded-session bug so hard to see.
        raw = next(
            (
                str(payload[k])
                for k in ("session_id", "thread_id", "conversation_id", "task_id")
                if payload.get(k)
            ),
            "-",
        )
        _log(
            f"event {source}/{event} -> {status} | payload_id={raw} "
            f"| key={session} | keys={sorted(sessions)}"
        )
        if status == "remove":
            sessions.pop(session, None)
        elif status != "ignore":
            sessions[session] = {
                "status": status,
                "source": source,
                "updated": now,
                "expires": expires,
                "event": event,
            }
        if status in {"remove", "done", "error"}:
            _drop_stranded(sessions, source, session)
        state["version"] = 2
        state["generation"] = int(state.get("generation", 0)) + 1
        _save_state(state)
        snapshot = state

    # Safety net: if no daemon is beating, drive the light from this process so
    # a wedged or missing daemon can't leave the keyboard silent.
    alive = _daemon_is_alive()
    if not alive:
        try:
            packet, _ = _packet_for(_active_effect(snapshot, now), now)
            send_packet(packet)
        except Exception as exc:  # Never break the agent over a light.
            _log(f"direct send failed: {exc!r}")
    _start_daemon(daemon_alive=alive)


def _start_daemon(daemon_alive: bool | None = None) -> None:
    # Skip the spawn only when a daemon is provably beating — it polls the
    # state file and will pick this event up. Checking mutex ownership alone
    # is not enough: a wedged holder would silently swallow every event.
    if daemon_alive if daemon_alive is not None else _daemon_is_alive():
        return
    # CREATE_NO_WINDOW and DETACHED_PROCESS are mutually exclusive per the
    # CreateProcess contract. DETACHED_PROCESS alone already gives us no
    # console window, so send only that flag.
    creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    command = [sys.executable, str(Path(__file__).resolve()), "_daemon"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
        )
        _log(f"spawned daemon pid={process.pid} flags={creation_flags:#x}")
    except Exception as exc:  # Never let a spawn failure break the agent.
        _log(f"failed to spawn daemon: {exc!r}")


def _active_effect(state: dict[str, Any], now: float) -> str:
    active: list[dict[str, Any]] = []
    for item in state.get("sessions", {}).values():
        expires = item.get("expires")
        if expires is not None and float(expires) <= now:
            continue
        active.append(item)
    statuses = {str(item.get("status", "working")) for item in active}
    for candidate in ("error", "approval", "done"):
        if candidate in statuses:
            return candidate
    sources = {
        str(item.get("source", ""))
        for item in active
        if item.get("status", "working") == "working"
    }
    if "codex" in sources and "claude" in sources:
        return "working-both"
    if "codex" in sources:
        return "working-codex"
    if "claude" in sources:
        return "working-claude"
    return "baseline"


def _read_heartbeat() -> tuple[int, float]:
    beat = _load_state().get("daemon") or {}
    try:
        return int(beat.get("pid", 0)), float(beat.get("beat", 0.0))
    except (TypeError, ValueError):
        return 0, 0.0


def _write_heartbeat(pid: int) -> bool:
    """Claim the heartbeat. False means another live daemon already owns it.

    After a takeover the wedged daemon can still be alive and beating. Letting
    both write left two loops driving the same HID device, and each new daemon
    deferring to whichever wrote last.
    """
    try:
        with _named_mutex(STATE_MUTEX):
            state = _load_state()
            current = state.get("daemon") or {}
            try:
                other = int(current.get("pid", 0) or 0)
                beat = float(current.get("beat", 0.0) or 0.0)
            except (TypeError, ValueError):
                other, beat = 0, 0.0
            if other and other != pid and time.time() - beat <= DAEMON_STALE_AFTER:
                return False
            state["daemon"] = {"pid": pid, "beat": time.time()}
            _save_state(state)
    except (OSError, TimeoutError):
        pass
    return True


def _daemon_is_alive() -> bool:
    """A daemon counts as alive only if it holds the mutex AND is beating."""
    if not _daemon_mutex_held():
        return False
    _, beat = _read_heartbeat()
    return (time.time() - beat) <= DAEMON_STALE_AFTER


def _prune_state(now: float) -> dict[str, Any]:
    with _named_mutex(STATE_MUTEX):
        state = _load_state()
        sessions = state.get("sessions", {})
        clean = {}
        for key, item in sessions.items():
            expires = item.get("expires")
            updated = float(item.get("updated", 0))
            if expires is not None and float(expires) <= now:
                continue
            if now - updated > 6 * 60 * 60:
                continue
            clean[key] = item
        if clean != sessions:
            state["sessions"] = clean
            _save_state(state)
        return state


def _packet_name(packet: bytes) -> str:
    if not _PACKET_NAMES:
        _PACKET_NAMES.update(
            {
                BASELINE_PACKET: "BASELINE",
                CODEX_WORKING_PACKET: "CODEX_WORKING",
                CLAUDE_WORKING_PACKET: "CLAUDE_WORKING",
                SUCCESS_PACKET: "SUCCESS",
                PERMISSION_PACKET: "PERMISSION",
                ERROR_PACKET: "ERROR",
                OFF_PACKET: "OFF",
            }
        )
    return _PACKET_NAMES.get(packet, "UNKNOWN")


def _packet_for(effect: str, now: float) -> tuple[bytes, float]:
    """Map a composed effect to the packet to send and the poll delay."""
    if effect == "approval":
        return PERMISSION_PACKET, 0.25
    if effect == "error":
        blink = ERROR_PACKET if int(now / 0.12) % 2 == 0 else OFF_PACKET
        return blink, 0.04
    if effect == "done":
        return SUCCESS_PACKET, 0.25
    if effect == "working-codex":
        return CODEX_WORKING_PACKET, 0.25
    if effect == "working-claude":
        return CLAUDE_WORKING_PACKET, 0.25
    if effect == "working-both":
        alternate = (
            CODEX_WORKING_PACKET
            if int(now / 2.0) % 2 == 0
            else CLAUDE_WORKING_PACKET
        )
        return alternate, 0.20
    return BASELINE_PACKET, 0.25


def run_daemon(inline: bool = False, verbose: bool = False) -> int:
    """Drive the lights from the shared state file.

    inline=True skips the single-instance mutex and is used by `simulate
    --inline` to run the loop inside the calling process. That isolates the
    detached-process spawn from the lighting logic itself.
    """

    def report(message: str) -> None:
        _log(message)
        if verbose:
            print(f"    [daemon] {message}", flush=True)

    report(f"daemon entered (pid={os.getpid()}, inline={inline})")
    handle = None
    if not inline:
        handle = kernel32.CreateMutexW(None, False, DAEMON_MUTEX)
        error = ctypes.get_last_error()
        if not handle:
            report(f"daemon: CreateMutexW failed (winerror={error})")
            return 1
        if error == ERROR_ALREADY_EXISTS:
            other_pid, beat = _read_heartbeat()
            age = time.time() - beat
            if age <= DAEMON_STALE_AFTER:
                kernel32.CloseHandle(handle)
                report(
                    f"daemon: live instance pid={other_pid} "
                    f"(heartbeat {age:.1f}s ago), exiting"
                )
                return 0
            # The holder is wedged. Take over rather than leaving lights dead.
            report(
                f"daemon: mutex held by pid={other_pid} but heartbeat is "
                f"{age:.0f}s stale — taking over"
            )

    report(f"daemon started (pid={os.getpid()}, inline={inline})")
    last_packet: bytes | None = None
    baseline_since: float | None = None
    last_beat = 0.0
    try:
        while True:
            if inline and _INLINE_STOP.is_set():
                report("daemon stopping (inline stop requested)")
                break
            now = time.time()
            if not inline and now - last_beat >= DAEMON_HEARTBEAT_INTERVAL:
                if not _write_heartbeat(os.getpid()):
                    report("daemon: another live daemon owns the heartbeat, stepping down")
                    break
                last_beat = now
            state = _prune_state(now)
            if state.get("_unreadable"):
                # Do not drop to baseline just because one read failed.
                time.sleep(0.2)
                continue
            effect = _active_effect(state, now)
            packet, delay = _packet_for(effect, now)
            baseline_since = (baseline_since or now) if effect == "baseline" else None

            if packet != last_packet:
                try:
                    send_packet(packet)
                    last_packet = packet
                    report(f"effect={effect} -> sent {_packet_name(packet)}")
                except Exception as exc:  # Keep hook failures out of Codex/Claude.
                    report(f"effect={effect} -> HID write FAILED: {exc!r}")

            if baseline_since is not None and now - baseline_since > 20.0:
                report("daemon exiting (baseline for 20s)")
                break
            time.sleep(delay)
    except Exception as exc:
        report(f"daemon crashed: {exc!r}")
        raise
    finally:
        # The error effect alternates ERROR/OFF. A daemon that exits on an OFF
        # frame leaves the keyboard dark with nothing left running to fix it,
        # which is exactly how the lights went out entirely.
        if last_packet is not None and last_packet != BASELINE_PACKET:
            try:
                send_packet(BASELINE_PACKET)
                report("daemon restored baseline on exit")
            except Exception as exc:
                report(f"daemon could not restore baseline: {exc!r}")
        if handle is not None:
            kernel32.CloseHandle(handle)
    return 0


def status() -> int:
    held = _daemon_mutex_held()
    pid, beat = _read_heartbeat()
    age = time.time() - beat if beat else None
    if not held:
        print("守护进程：无（互斥量空闲）")
    elif age is not None and age <= DAEMON_STALE_AFTER:
        print(f"守护进程：存活 pid={pid}（{age:.1f}s 前心跳）")
    else:
        stale = f"{age:.0f}s 前" if age is not None else "从未"
        print(f"守护进程：**卡死** pid={pid}（心跳 {stale}）—— 跑 stop 清掉")

    now = time.time()
    state = _load_state()
    sessions = state.get("sessions", {})
    print(f"generation = {state.get('generation')}，活动会话 {len(sessions)} 个")
    for key, item in sessions.items():
        expires = item.get("expires")
        left = f"，剩余 {float(expires) - now:.1f}s" if expires else ""
        age = now - float(item.get("updated", now))
        print(f"  {key}: status={item.get('status')} event={item.get('event')} "
              f"（{age:.0f}s 前更新{left}）")
    print(f"当前应显示的灯效：{_active_effect(state, now)}")

    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"\n日志尾部（共 {len(lines)} 行）：")
        for line in lines[-20:]:
            print(f"  {line}")
    else:
        print("\n没有日志文件。")
    return 0


def stop_daemon() -> int:
    """Clear stuck sessions and kill any lingering daemon process."""
    with _named_mutex(STATE_MUTEX):
        state = _load_state()
        count = len(state.get("sessions", {}))
        state["sessions"] = {}
        state.pop("daemon", None)
        state["generation"] = int(state.get("generation", 0)) + 1
        _save_state(state)
    print(f"已清空 {count} 个会话状态。")

    killed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process "
            "| Where-Object { $_.CommandLine -like '*keyboard_lights.py*_daemon*' } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }",
        ],
        capture_output=True,
        text=True,
    )
    pids = [line.strip() for line in killed.stdout.splitlines() if line.strip()]
    print(f"已结束 {len(pids)} 个残留 daemon 进程{'：' + ', '.join(pids) if pids else ''}。")

    time.sleep(0.5)
    print(f"互斥量：{'仍被占用' if _daemon_mutex_held() else '已释放'}")
    send_packet(BASELINE_PACKET)
    print("已恢复青绿色单点模式。")
    return 0


def probe() -> int:
    matching = [
        path
        for path in _enumerate_hid_paths()
        if f"vid_{VID:04x}&pid_{PID:04x}" in path.lower()
        and INTERFACE_MARKER in path.lower()
    ]
    if not matching:
        print(f"未找到 {DEVICE.name} 的灯光接口（配置 {DEVICE.key}，{DEVICE.hid_id}）。")
        print("若你的键盘是别的型号，见 README「换设备重新抓包」。")
        return 1
    print(f"已找到 {DEVICE.name} 灯光接口（配置 {DEVICE.key}）：")
    for path in matching:
        print(path)
    print(
        f"{len(DEVICE.packets)} 组灯效数据均为 {DEVICE.report_length} 字节，"
        "并已通过安全检查。"
    )
    return 0


def list_devices() -> int:
    devices = load_devices()
    try:
        paths = _enumerate_hid_paths()
    except OSError:
        paths = []
    print(f"设备配置目录：{DEVICE_DIR}")
    for key, device in sorted(devices.items()):
        marks = []
        if device.is_present(paths):
            marks.append("已插入")
        if key == DEVICE.key:
            marks.append("当前使用")
        flag = ("  <- " + "、".join(marks)) if marks else ""
        print(f"  {key:<16} {device.name} ({device.hid_id}){flag}")
    return 0


def show_effect(effect: str, seconds: float) -> None:
    duration = max(0.1, seconds)
    if effect == "error":
        deadline = time.time() + duration
        lit = True
        while time.time() < deadline:
            send_packet(ERROR_PACKET if lit else OFF_PACKET)
            lit = not lit
            time.sleep(0.12)
    else:
        packets = {
            "codex": CODEX_WORKING_PACKET,
            "claude": CLAUDE_WORKING_PACKET,
            "success": SUCCESS_PACKET,
            "permission": PERMISSION_PACKET,
        }
        send_packet(packets[effect])
        time.sleep(duration)
    send_packet(BASELINE_PACKET)


def simulate(source: str, inline: bool = False) -> int:
    """Replay a realistic agent session through the real hook code path.

    This exercises record_event -> state.json -> daemon exactly like a live
    hook would, so it validates the whole chain without needing Codex or
    Claude to actually run. Useful when an agent's quota is exhausted.
    """
    session = f"simulate-{os.getpid()}"
    script: list[tuple[str, str, float]] = [
        ("SessionStart", "会话开始，应保持青绿色（开着不等于在干活）", 2.0),
        ("UserPromptSubmit", "提交提示词，继续跑马灯", 2.0),
        ("PreToolUse", "工具执行前，继续跑马灯", 2.0),
        ("PermissionRequest", "等待确认，应转为紫色呼吸", 4.0),
        ("PostToolUse", "已批准，回到跑马灯", 3.0),
        ("Stop", "任务完成，应转为绿色常亮 10 秒", 12.0),
    ]
    colors = {
        "codex": "蓝色",
        "claude": "橙色",
    }
    print(f"模拟 {source} 会话（工作状态应为{colors.get(source, '对应')}跑马灯）")
    if inline:
        print("模式：inline —— 灯光循环跑在本进程内，会打印每一次 HID 写入\n")
    else:
        print("模式：分离进程 —— 与真实 hook 完全一致\n")
    print("请盯着键盘，对照每一步的预期灯效：\n")

    worker: threading.Thread | None = None
    if inline:
        _INLINE_STOP.clear()
        worker = threading.Thread(
            target=run_daemon, kwargs={"inline": True, "verbose": True}, daemon=True
        )
        worker.start()

    try:
        for event, expectation, pause in script:
            payload = {
                "session_id": session,
                "hook_event_name": event,
                "cwd": str(Path.cwd()),
            }
            if inline:
                # Write state directly; _start_daemon would spawn a rival loop.
                _record_event_only(source, event, payload)
            else:
                record_event(source, event, payload)
            print(f"  -> {event:<18} {expectation}")
            time.sleep(pause)

        if inline:
            _record_event_only(source, "SessionEnd", {"session_id": session})
        else:
            record_event(source, "SessionEnd", {"session_id": session})
        print("\n  -> SessionEnd         已清理，应恢复青绿色单点模式")
        time.sleep(2.0)
    finally:
        if worker is not None:
            _INLINE_STOP.set()
            worker.join(timeout=5.0)

    print("\n模拟结束。")
    if inline:
        print("上面每条 [daemon] 行都是一次真实的 HID 写入。")
        print("若有 [daemon] 输出但灯不动 —— 问题在灯效指令本身。")
        print("若连 [daemon] 输出都没有 —— 问题在状态机。")
    else:
        print("若灯效都对，说明整条链路正常，问题只剩「agent 有没有真的调 hook」。")
        print("若灯完全不动，改跑：python .\\keyboard_lights.py simulate claude --inline")
    return 0


def _record_event_only(source: str, event: str, payload: dict[str, Any]) -> None:
    """record_event without spawning the detached daemon."""
    status, expires = _normalize_event(event, payload)
    now = time.time()
    with _named_mutex(STATE_MUTEX):
        state = _load_state()
        sessions = state.setdefault("sessions", {})
        session = _resolve_session(source, payload, sessions, now)
        # One line per incoming hook. The effect log alone cannot tell a
        # mis-mapped event from a mis-rendered light, which is what made the
        # stranded-session bug so hard to see.
        raw = next(
            (
                str(payload[k])
                for k in ("session_id", "thread_id", "conversation_id", "task_id")
                if payload.get(k)
            ),
            "-",
        )
        _log(
            f"event {source}/{event} -> {status} | payload_id={raw} "
            f"| key={session} | keys={sorted(sessions)}"
        )
        if status == "remove":
            sessions.pop(session, None)
        elif status != "ignore":
            sessions[session] = {
                "status": status,
                "source": source,
                "updated": now,
                "expires": expires,
                "event": event,
            }
        if status in {"remove", "done", "error"}:
            _drop_stranded(sessions, source, session)
        state["version"] = 2
        state["generation"] = int(state.get("generation", 0)) + 1
        _save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="K500-M81 任务灯光提示")
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser.add_argument(
        "--device",
        help="指定 devices/ 里的配置名（默认按已插入的 VID/PID 自动选择）",
    )
    subparsers.add_parser("probe", help="只检查设备，不改变灯光")
    subparsers.add_parser("devices", help="列出可用的键盘配置")
    subparsers.add_parser("restore", help="恢复校准时的原灯效")
    subparsers.add_parser("status", help="查看 daemon、状态与日志")
    subparsers.add_parser("stop", help="清空卡住的会话状态并恢复基线")
    simulate_parser = subparsers.add_parser(
        "simulate", help="走真实 hook 路径重放一次会话，不需要 agent"
    )
    simulate_parser.add_argument(
        "source", nargs="?", default="claude", choices=("codex", "claude")
    )
    simulate_parser.add_argument(
        "--inline",
        action="store_true",
        help="在本进程内跑灯光循环并打印每次 HID 写入（绕开分离子进程）",
    )
    demo_parser = subparsers.add_parser("demo", help="播放一次成功提示并恢复")
    demo_parser.add_argument("--seconds", type=float, default=3.0)
    show_parser = subparsers.add_parser("show", help="播放指定状态并恢复")
    show_parser.add_argument(
        "effect", choices=("codex", "claude", "success", "permission", "error")
    )
    show_parser.add_argument("--seconds", type=float, default=3.0)
    hook_parser = subparsers.add_parser("hook", help=argparse.SUPPRESS)
    hook_parser.add_argument("source", choices=("codex", "claude", "manual"))
    hook_parser.add_argument("event")
    hook_parser.add_argument("extra", nargs="*")
    subparsers.add_parser("_daemon", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.device:
        use_device(select_device(args.device))

    if args.command == "probe":
        return probe()
    if args.command == "devices":
        return list_devices()
    if args.command == "restore":
        send_packet(BASELINE_PACKET)
        print("已恢复青绿色单点模式。")
        return 0
    if args.command == "status":
        return status()
    if args.command == "stop":
        return stop_daemon()
    if args.command == "simulate":
        return simulate(args.source, inline=args.inline)
    if args.command == "demo":
        show_effect("success", args.seconds)
        print("成功提示测试完成，已恢复单点模式。")
        return 0
    if args.command == "show":
        show_effect(args.effect, args.seconds)
        print(f"{args.effect} 灯效测试完成，已恢复单点模式。")
        return 0
    if args.command == "hook":
        payload = _read_hook_payload(args.extra)
        record_event(args.source, args.event, payload)
        if args.source == "codex" and args.event.lower() in {"stop", "subagentstop"}:
            # Codex Stop-family hooks require valid JSON on stdout.
            print("{}")
        return 0
    if args.command == "_daemon":
        # The detached process has no console, so any failure here would be
        # invisible without this guard.
        try:
            return run_daemon()
        except BaseException as exc:
            _log(f"daemon: unhandled {exc!r}")
            raise
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
