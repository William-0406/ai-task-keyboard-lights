"""K500-M81 task lighting: HID layer, device profiles, and the CLI.

Only the verified 0x07 lighting command is emitted. No firmware, key-map, or
Hall-effect configuration commands are implemented in this program.

The moving parts live in three modules with one job each:

- light_client.py  — hook client: appends one event line, exits.
- light_service.py — the single resident service: state machine + HID writes.
- this file        — HID plumbing, device profiles, and the CLI entry points.

Hooks never write HID and never start daemons. The old per-agent daemon and
the shared state.json are gone: sandboxes gave each agent a private
copy-on-write view of that file (same path, different inodes, measured), so
any design that decided lights from cross-sandbox shared state lost updates.
Only the append-only event log has been shown to cross both sandboxes.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any


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
LOG_FILE = APP_DIR / "keyboard-lights.log"
SERVICE_TASK_NAME = "MachenikeTaskLights"

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
    try:
        paths = _enumerate_hid_paths()
    except OSError:
        paths = []
    for device in devices.values():
        if device.is_present(paths):
            return device
    # Nothing plugged in matches any profile. Fall back to the first one so
    # `devices` and `probe` still have something to talk about, but callers
    # must not assume the keyboard is there -- ask device_present(). Skipping
    # this check when only one profile exists is what made a fresh clone on
    # any other keyboard fail silently: the service started, reported healthy,
    # and wrote "HID write FAILED" to a log nobody was looking at.
    return next(iter(devices.values()))


def device_present(device: "Device | None" = None) -> bool:
    """True when the selected profile's keyboard is actually plugged in."""
    target = device if device is not None else DEVICE
    try:
        return target.is_present(_enumerate_hid_paths())
    except OSError:
        return False


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


def record_event(source: str, event: str, payload: dict[str, Any]) -> None:
    """Compatibility entry (used by watch_cowork.py): append one event.

    This no longer writes HID or spawns anything — the resident service picks
    the event up from events.jsonl like any hook event.
    """
    import light_client

    light_client.append_event(light_client.build_event(source, event, payload))


# --- service management ---------------------------------------------------

def _service_command() -> list[str]:
    """The command line that runs the resident service, console-free."""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else python
    return [str(interpreter), str(Path(__file__).resolve()), "service", "run"]


def _startup_launcher() -> Path:
    startup = (
        Path(os.environ.get("APPDATA", Path.home()))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    return startup / f"{SERVICE_TASK_NAME}.vbs"


def service_install() -> int:
    """Register the service to start at logon, outside every sandbox.

    A Scheduled Task is tried first; creating an ONLOGON task needs elevation
    on some Windows 11 machines, so a Startup-folder launcher (equally outside
    the sandboxes — Explorer starts it in the real logon session) is the
    fallback.
    """
    command = _service_command()
    tr = " ".join(f'"{part}"' for part in command)
    result = subprocess.run(
        [
            "schtasks", "/Create", "/F",
            "/TN", SERVICE_TASK_NAME,
            "/SC", "ONLOGON",
            "/TR", tr,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"已注册登录自启计划任务 {SERVICE_TASK_NAME}：{tr}")
        print("现在执行 service start 立即启动（无需重新登录）。")
        return 0
    print(f"计划任务创建被拒绝（{result.stderr.strip() or result.stdout.strip()}），改用启动文件夹。")
    launcher = _startup_launcher()
    # Inside a VBScript string literal a doubled "" is one quote character.
    quoted = " ".join(f'""{part}""' for part in command)
    script = f'CreateObject("Wscript.Shell").Run "{quoted}", 0, False\n'
    try:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        # wscript reads .vbs as ANSI unless there is a UTF-16 BOM; the repo
        # path contains non-ASCII characters, so the BOM is load-bearing.
        launcher.write_text(script, encoding="utf-16")
    except OSError as exc:
        print(f"启动文件夹写入也失败：{exc}")
        return 1
    print(f"已写入登录启动项：{launcher}")
    print("现在执行 service start 立即启动（无需重新登录）。")
    return 0


def service_uninstall() -> int:
    service_stop()
    result = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", SERVICE_TASK_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"已删除计划任务 {SERVICE_TASK_NAME}。")
    launcher = _startup_launcher()
    if launcher.exists():
        try:
            launcher.unlink()
            print(f"已删除登录启动项 {launcher}。")
        except OSError as exc:
            print(f"启动项删除失败：{exc}")
    return 0


def _service_running() -> bool:
    """Mutex first; snapshot freshness as fallback for cross-session checks."""
    import light_service

    if light_service.service_mutex_held():
        return True
    try:
        snap = json.loads(light_service.SNAPSHOT_FILE.read_text(encoding="utf-8"))
        return time.time() - float(snap.get("written_at", 0.0)) < 6.0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def service_start() -> int:
    if _service_running():
        print("灯效服务已在运行。")
        return 0
    # Prefer Task Scheduler: it launches the process in the user's real logon
    # session, outside whatever sandbox this shell may be running in — which
    # is the entire point of the unified service.
    via_task = subprocess.run(
        ["schtasks", "/Run", "/TN", SERVICE_TASK_NAME],
        capture_output=True,
        text=True,
    )
    how = f"计划任务 {SERVICE_TASK_NAME}"
    if via_task.returncode != 0:
        how = "本进程派生（计划任务不可用，建议先 service install）"
        subprocess.Popen(
            _service_command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    for _ in range(50):
        time.sleep(0.1)
        if _service_running():
            print(f"灯效服务已启动（{how}）。")
            return 0
    print(f"已尝试通过{how}启动，但 5 秒内未确认存活，查看 service.log。")
    return 1


def service_stop() -> int:
    import light_service

    if not _service_running():
        print("没有运行中的灯效服务。")
        return 0
    if light_service.request_stop():
        for _ in range(80):
            time.sleep(0.1)
            if not _service_running():
                print("灯效服务已停止。")
                return 0
        print("已发送停止信号，但服务 8 秒内未退出，改为按 PID 精确结束。")
    # The named stop event may not cross a sandbox boundary; fall back to a
    # PID kill, verified against the snapshot AND the full command line so we
    # can never terminate an unrelated process.
    try:
        snap = json.loads(light_service.SNAPSHOT_FILE.read_text(encoding="utf-8"))
        pid = int(snap.get("pid", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pid = 0
    if not pid:
        print("找不到服务 PID，无法结束——看 service status。")
        return 1
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($p -and $p.CommandLine -like '*keyboard_lights.py*service*run*') "
            "{ Stop-Process -Id $p.ProcessId -Force; 'killed' } else { 'mismatch' }",
        ],
        capture_output=True,
        text=True,
    )
    if "killed" in result.stdout:
        print(f"已结束服务进程 pid={pid}。")
        return 0
    print(f"pid={pid} 的命令行与灯效服务不匹配，已放弃（不误杀）。")
    return 1


def service_status() -> int:
    import light_service

    if light_service.service_mutex_held():
        print("服务：运行中（互斥量被持有）")
    elif _service_running():
        print("服务：运行中（本会话看不到互斥量，但快照仍在心跳）")
    else:
        print("服务：未运行")
    snapshot_file = light_service.SNAPSHOT_FILE
    if snapshot_file.exists():
        try:
            snap = json.loads(snapshot_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"快照读取失败：{exc}")
            snap = {}
        if snap:
            present = snap.get("device_present")
            name = snap.get("device_name") or snap.get("device") or "?"
            if present is False:
                print(f"** 键盘未插入：找不到 {name} —— 灯效不可能工作 **")
                print("   跑 `probe` 看接口，或见 README「换设备：加一份设备配置」。")
            elif present:
                print(f"键盘：{name}（已插入）")
            failures = snap.get("hid_failures") or 0
            if failures:
                print(f"** 连续 {failures} 次 HID 写入失败 **")
                if snap.get("hid_last_error"):
                    print(f"   最后一次错误：{snap['hid_last_error']}")
            written = float(snap.get("written_at", 0.0))
            started = float(snap.get("started_at", 0.0))
            print(
                f"PID {snap.get('pid')}，启动于 "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}，"
                f"快照 {time.time() - written:.0f} 秒前"
            )
            print(f"最近处理的事件：{snap.get('last_event_id') or '（无）'}")
            print(f"当前决定的灯效：{snap.get('effect')}")
            sessions = snap.get("sessions") or []
            print(f"活动会话 {len(sessions)} 个：")
            for item in sessions:
                agent = f" agent={item['agent']}" if item.get("agent") else ""
                lease = (
                    f"，租约剩余 {item['lease_left']:.0f}s"
                    if item.get("lease_left") is not None
                    else ""
                )
                print(
                    f"  {item.get('source')}:{item.get('session')}{agent}: "
                    f"status={item.get('status')} event={item.get('event')} "
                    f"（{item.get('updated_ago')}s 前更新{lease}）"
                )
    else:
        print("还没有 service-state.json 快照。")

    log = light_service.SERVICE_LOG
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"\nservice.log 尾部（共 {len(lines)} 行）：")
        for line in lines[-15:]:
            print(f"  {line}")
    return 0


def kill_legacy_daemons() -> int:
    """End leftover pre-service `_daemon` processes, matched precisely."""
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process "
            "| Where-Object { $_.CommandLine -like '*keyboard_lights.py*_daemon*' } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }",
        ],
        capture_output=True,
        text=True,
    )
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if pids:
        print(f"已结束 {len(pids)} 个旧版 daemon 进程：{', '.join(pids)}")
    else:
        print("没有旧版 daemon 进程。")
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


def simulate(source: str, inline: bool = False) -> int:
    """Replay a realistic agent session through the real event pipeline.

    Events go into events.jsonl exactly as a live hook writes them; the
    resident service renders them. --inline runs the service loop inside this
    process instead (refusing if a real service holds the mutex), which
    isolates state-machine issues from service-lifecycle issues.
    """
    import light_client
    import light_service

    session = f"simulate-{os.getpid()}"
    script: list[tuple[str, str, float]] = [
        ("SessionStart", "会话开始，应保持青绿色（开着不等于在干活）", 2.0),
        ("UserPromptSubmit", "提交提示词，开始跑马灯", 2.0),
        ("PreToolUse", "工具执行前，继续跑马灯", 2.0),
        ("PermissionRequest", "等待确认，应转为紫色呼吸", 4.0),
        ("PostToolUse", "已批准，回到跑马灯", 3.0),
        ("Stop", "任务完成，应转为绿色常亮 10 秒", 12.0),
    ]
    colors = {"codex": "蓝色", "claude": "橙色"}
    print(f"模拟 {source} 会话（工作状态应为{colors.get(source, '对应')}跑马灯）")

    worker: threading.Thread | None = None
    stop_flag = threading.Event()
    if inline:
        if light_service.service_mutex_held():
            print("常驻服务已在运行——inline 模式会和它抢 HID，请先 service stop。")
            return 1
        print("模式：inline —— 服务循环跑在本进程内\n")
        worker = threading.Thread(
            target=light_service.run_service,
            kwargs={"verbose": True, "stop_check": stop_flag.is_set},
            daemon=True,
        )
        worker.start()
        time.sleep(0.5)
    elif not light_service.service_mutex_held():
        print("警告：常驻服务未运行，事件会入队但不会亮灯。先跑 service start。\n")
    else:
        print("模式：写入 events.jsonl，由常驻服务渲染 —— 与真实 hook 完全一致\n")
    print("请盯着键盘，对照每一步的预期灯效：\n")

    try:
        for event, expectation, pause in script:
            payload = {"session_id": session, "hook_event_name": event}
            light_client.append_event(light_client.build_event(source, event, payload))
            print(f"  -> {event:<18} {expectation}")
            time.sleep(pause)
        light_client.append_event(
            light_client.build_event(source, "SessionEnd", {"session_id": session})
        )
        print("\n  -> SessionEnd         已清理，应恢复青绿色单点模式")
        time.sleep(2.0)
    finally:
        if worker is not None:
            stop_flag.set()
            worker.join(timeout=5.0)

    print("\n模拟结束。")
    print("若灯效都对，链路正常；若不对，先看 service status 和 service.log。")
    return 0


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
    subparsers.add_parser("status", help="等同于 service status")
    subparsers.add_parser("stop", help="停止服务、清理旧 daemon、恢复基线")
    service_parser = subparsers.add_parser("service", help="管理统一灯效服务")
    service_parser.add_argument(
        "action",
        choices=("install", "uninstall", "start", "stop", "status", "run"),
    )
    simulate_parser = subparsers.add_parser(
        "simulate", help="走真实事件管道重放一次会话，不需要 agent"
    )
    simulate_parser.add_argument(
        "source", nargs="?", default="claude", choices=("codex", "claude")
    )
    simulate_parser.add_argument(
        "--inline",
        action="store_true",
        help="在本进程内跑服务循环并打印每次 HID 写入（绕开常驻服务）",
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
        return service_status()
    if args.command == "stop":
        service_stop()
        kill_legacy_daemons()
        send_packet(BASELINE_PACKET)
        print("已恢复青绿色单点模式。")
        return 0
    if args.command == "service":
        import light_service

        if args.action == "install":
            return service_install()
        if args.action == "uninstall":
            return service_uninstall()
        if args.action == "start":
            return service_start()
        if args.action == "stop":
            return service_stop()
        if args.action == "status":
            return service_status()
        if args.action == "run":
            # Under pythonw.exe sys.stdout is None — never touch isatty on it.
            verbose = bool(sys.stdout) and sys.stdout.isatty()
            return light_service.run_service(verbose=verbose)
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
        # The hook client appends one event line and exits. It must never
        # write HID, spawn processes, or fail the agent's own task.
        import light_client

        light_client.handle_hook(args.source, args.event, args.extra)
        if args.source == "codex" and args.event.lower() in {"stop", "subagentstop"}:
            # Codex Stop-family hooks require valid JSON on stdout.
            print("{}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
