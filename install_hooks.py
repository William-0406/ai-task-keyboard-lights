"""Merge/unmerge keyboard-light hooks without replacing existing hooks.

Run inside WSL with --wsl to wire up the Claude Code session that the desktop
app's Code tab starts there. See `_wsl_hook` for why that mode needs both a
Linux path and a Windows path for what is nominally the same command.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parent
CONTROLLER = ROOT / "keyboard_lights.py"
CODEX_SETTINGS = Path.home() / ".codex" / "hooks.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
MARKER = "keyboard_lights.py"

CODEX_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
)

CLAUDE_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Notification",
    "Elicitation",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
)


def running_in_wsl() -> bool:
    if os.name == "nt":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="replace").lower()
    except OSError:
        return False


def _wslpath(path: str, to_windows: bool) -> str | None:
    """Convert between /mnt/c/... and C:\\... using WSL's own converter."""
    try:
        result = subprocess.run(
            ["wslpath", "-w" if to_windows else "-u", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def find_windows_python() -> str | None:
    """Locate a Windows python.exe as seen from inside WSL."""
    patterns = [
        "/mnt/c/Python3*/python.exe",
        "/mnt/c/Program Files/Python3*/python.exe",
        "/mnt/c/Users/*/AppData/Local/Programs/Python/Python3*/python.exe",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    # Newest interpreter last in sort order, prefer it.
    return sorted(found)[-1] if found else None


def _wsl_hook(event: str, linux_python: str, windows_controller: str) -> dict[str, Any]:
    """A hook that fires in Linux but executes as a Windows process.

    The two paths are deliberately different shapes. `command` is executed by
    Linux, so it must be the /mnt/c/... path that WSL's binfmt interop can
    launch. Everything in `args` is handed to that Windows process, so the
    script path must be a native C:\\... path — a /mnt/c/... argument would be
    meaningless to python.exe and fail with "No such file or directory".
    """
    return {
        "type": "command",
        "command": linux_python,
        "args": [windows_controller, "hook", "claude", event],
        "timeout": 5,
        "statusMessage": "同步键盘提示灯",
    }


def _merge_wsl(windows_python: str | None) -> bool:
    linux_python = windows_python or find_windows_python()
    if not linux_python:
        print("找不到 Windows 的 python.exe。用 --windows-python 指定，例如：")
        print("  python3 install_hooks.py --wsl --windows-python /mnt/c/Python314/python.exe")
        return False
    if not Path(linux_python).exists():
        print(f"路径不存在：{linux_python}")
        return False

    windows_controller = _wslpath(str(CONTROLLER), to_windows=True)
    if not windows_controller:
        print(f"无法把 {CONTROLLER} 转换成 Windows 路径。")
        print("请把本仓库放在 C 盘（WSL 下形如 /mnt/c/...）后重试。")
        return False

    print(f"Linux 侧解释器：{linux_python}")
    print(f"Windows 侧脚本：{windows_controller}")

    settings = _load(CLAUDE_SETTINGS)
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in CLAUDE_EVENTS:
        groups = hooks.setdefault(event, [])
        expected = _wsl_hook(event, linux_python, windows_controller)
        existing = None
        for group in groups:
            if _has_marker(group):
                existing = group
                break
        if existing is not None:
            old_hooks = existing.get("hooks", [])
            kept = [
                hook
                for hook in old_hooks
                if not (
                    isinstance(hook, dict)
                    and MARKER in f"{hook.get('command', '')} {hook.get('args', '')}"
                )
            ]
            desired = kept + [expected]
            if desired != old_hooks:
                existing["hooks"] = desired
                changed = True
            continue
        groups.append({"matcher": "", "hooks": [expected]})
        changed = True

    if changed:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        if CLAUDE_SETTINGS.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = CLAUDE_SETTINGS.with_name(
                f"settings.keyboard-lights-backup-{stamp}.json"
            )
            shutil.copy2(CLAUDE_SETTINGS, backup)
        CLAUDE_SETTINGS.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return changed


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 的顶层不是 JSON 对象")
    return value


def _command(source: str, event: str) -> str:
    # Quote both paths: either can contain spaces (Program Files) or
    # non-ASCII characters, and shell-form hooks get tokenized by a shell.
    python = Path(sys.executable).resolve().as_posix()
    controller = CONTROLLER.as_posix()
    return f'"{python}" "{controller}" hook {source} {event}'


def _timeout(source: str, event: str) -> int:
    # Codex caps SessionEnd hooks at 3 seconds; a larger value is rejected.
    if source == "codex" and event == "SessionEnd":
        return 3
    return 5


def _hook(source: str, event: str) -> dict[str, Any]:
    python = Path(sys.executable).resolve().as_posix()
    controller = CONTROLLER.as_posix()
    hook: dict[str, Any] = {
        "type": "command",
        "command": _command(source, event),
        "timeout": _timeout(source, event),
    }
    if source == "codex":
        hook["commandWindows"] = _command(source, event)
        hook["statusMessage"] = "同步键盘提示灯"
    else:
        # Exec form bypasses shell parsing and preserves the Unicode path.
        hook["command"] = python
        hook["args"] = [controller, "hook", source, event]
        hook["statusMessage"] = "同步键盘提示灯"
    return hook


def _has_marker(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []):
        if isinstance(hook, dict):
            surface = f"{hook.get('command', '')} {hook.get('args', '')}"
            if MARKER in surface:
                return True
    return False


def _merge(path: Path, source: str, events: tuple[str, ...]) -> bool:
    settings = _load(path)
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in events:
        groups = hooks.setdefault(event, [])
        expected = _hook(source, event)
        existing = None
        for group in groups:
            if _has_marker(group):
                existing = group
                break
        if existing is not None:
            old_hooks = existing.get("hooks", [])
            kept = [
                hook
                for hook in old_hooks
                if not (isinstance(hook, dict) and MARKER in str(hook.get("command", "")))
            ]
            # Exec-form Claude hooks keep the controller path in args.
            kept = [
                hook
                for hook in kept
                if not (isinstance(hook, dict) and MARKER in str(hook.get("args", "")))
            ]
            desired = kept + [expected]
            if desired != old_hooks:
                existing["hooks"] = desired
                changed = True
            continue
        group: dict[str, Any] = {"hooks": [expected]}
        if source == "claude":
            group["matcher"] = ""
        groups.append(group)
        changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.stem}.keyboard-lights-backup-{stamp}{path.suffix}")
            shutil.copy2(path, backup)
        path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return changed


def _unmerge(path: Path) -> bool:
    settings = _load(path)
    hooks = settings.get("hooks", {})
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _has_marker(group)]
        if len(kept) != len(groups):
            hooks[event] = kept
            changed = True
    if changed:
        path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 Codex / Claude 键盘提示灯 Hooks")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--wsl",
        action="store_true",
        help="在 WSL 内运行：为桌面版 Code 标签页安装 Hook（经 interop 调用 Windows Python）",
    )
    parser.add_argument(
        "--windows-python",
        help="WSL 视角下 Windows python.exe 的路径，如 /mnt/c/Python314/python.exe",
    )
    args = parser.parse_args()

    if args.wsl:
        if os.name == "nt":
            print("--wsl 需要在 WSL 里运行，不是在 Windows 的 PowerShell 里。")
            print("先开 WSL（命令 wsl），cd 到本仓库，再执行：")
            print("  python3 install_hooks.py --wsl")
            return 1
        if not running_in_wsl():
            print("警告：当前不像是 WSL 环境，仍按 WSL 模式继续。")
        if args.uninstall:
            removed = _unmerge(CLAUDE_SETTINGS)
            print(f"WSL Claude：{'已移除' if removed else '无需修改'}")
            return 0
        changed = _merge_wsl(args.windows_python)
        print(f"WSL Claude：{'已安装' if changed else '已经安装（或安装失败，见上）'}")
        print(f"配置文件：{CLAUDE_SETTINGS}")
        print("重开桌面版 Code 会话后生效。")
        return 0

    if args.uninstall:
        codex = _unmerge(CODEX_SETTINGS)
        claude = _unmerge(CLAUDE_SETTINGS)
        print(f"Codex：{'已移除' if codex else '无需修改'}")
        print(f"Claude：{'已移除' if claude else '无需修改'}")
        return 0

    codex = _merge(CODEX_SETTINGS, "codex", CODEX_EVENTS)
    claude = _merge(CLAUDE_SETTINGS, "claude", CLAUDE_EVENTS)
    print(f"Codex：{'已安装' if codex else '已经安装'}")
    print(f"Claude：{'已安装' if claude else '已经安装'}")
    print("原有 Hooks 已保留。重新打开 Codex / Claude 后生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
