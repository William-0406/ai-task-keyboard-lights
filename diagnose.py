r"""端到端诊断：定位灯效"手动能亮、hook 不触发"断在哪一环。

用法：
    python .\diagnose.py

只读检查 + 一次模拟 hook 调用，不会修改 hooks.json / settings.json。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
CONTROLLER = ROOT / "keyboard_lights.py"
CODEX_SETTINGS = Path.home() / ".codex" / "hooks.json"
CODEX_TOML = Path.home() / ".codex" / "config.toml"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MachenikeTaskLights"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "keyboard-lights.log"
MARKER = "keyboard_lights.py"

OK = "  [OK]  "
BAD = "  [!!]  "
INFO = "  [--]  "


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def _iter_hooks(settings: dict) -> list[tuple[str, dict]]:
    found = []
    for event, groups in (settings.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []) or []:
                if isinstance(hook, dict):
                    found.append((event, hook))
    return found


def check_env() -> None:
    section("1. 环境")
    print(f"{INFO}sys.executable = {sys.executable}")
    if " " in sys.executable:
        print(f"{BAD}Python 路径含空格 —— shell 形式的 hook 命令必须加引号，否则会断")
    else:
        print(f"{OK}Python 路径不含空格")
    print(f"{INFO}控制器路径 = {CONTROLLER}")
    print(f"{OK if CONTROLLER.exists() else BAD}keyboard_lights.py {'存在' if CONTROLLER.exists() else '缺失'}")
    non_ascii = [c for c in str(CONTROLLER) if ord(c) > 127]
    if non_ascii:
        print(f"{INFO}路径含非 ASCII 字符 {''.join(sorted(set(non_ascii)))} —— exec 形式(args)安全，shell 形式看代码页")


def check_settings(path: Path, label: str) -> int:
    section(f"2. {label} 配置：{path}")
    if not path.exists():
        print(f"{BAD}文件不存在 —— hook 根本没装上。跑 python .\\install_hooks.py")
        return 0
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        print(f"{BAD}JSON 解析失败：{exc}")
        print(f"{BAD}整份配置会被忽略，所有 hook 都不会触发")
        return 0
    if not isinstance(settings, dict):
        print(f"{BAD}顶层不是 JSON 对象")
        return 0

    hooks = _iter_hooks(settings)
    mine = [
        (event, hook)
        for event, hook in hooks
        if MARKER in f"{hook.get('command', '')} {hook.get('args', '')}"
    ]
    others = len(hooks) - len(mine)
    print(f"{INFO}文件里共 {len(hooks)} 个 hook handler，其中本项目 {len(mine)} 个，其它 {others} 个")

    if not mine:
        print(f"{BAD}没找到本项目的 hook —— 没装上，或被别的工具覆盖了")
        return 0

    events = sorted({event for event, _ in mine})
    print(f"{OK}已注册事件：{', '.join(events)}")

    for event, hook in mine[:2]:
        print(f"{INFO}示例（{event}）：{json.dumps(hook, ensure_ascii=False)}")

    # 逐条校验
    for event, hook in mine:
        if hook.get("type") != "command":
            print(f"{BAD}{event}: type 不是 command")
        cmd = hook.get("command", "")
        if "args" not in hook:
            # shell 形式：可执行文件路径必须被引号包住
            if cmd.startswith(("'", '"')):
                continue
            head = cmd.split(" ", 1)[0]
            if " " in cmd and not Path(head).exists():
                print(f"{BAD}{event}: shell 形式命令的可执行文件路径没加引号，可能被截断 -> {head}")
        else:
            exe = Path(hook["command"])
            if not exe.exists() and exe.name == exe.as_posix():
                print(f"{INFO}{event}: exec 形式依赖 PATH 解析 {exe}")
            elif not exe.exists():
                print(f"{BAD}{event}: exec 形式的可执行文件不存在 -> {exe}")
    return len(mine)


def check_codex_trust() -> None:
    section("3. Codex hook 信任状态（最可能的元凶）")
    print(f"{INFO}Codex 官方规则：非托管 hook 写进配置后处于「待审核」状态，")
    print(f"{INFO}必须在 CLI 里执行 /hooks 逐条 review + trust 之后才会真正运行。")
    print(f"{INFO}hook 定义的哈希一变（比如重装、改了命令），信任就作废，需要重新审核。")
    print()
    print(f"{INFO}请在 Codex CLI 里输入 /hooks，确认本项目的 hook 是 trusted 还是 needs review。")
    if CODEX_TOML.exists():
        text = CODEX_TOML.read_text(encoding="utf-8-sig", errors="replace")
        if "[hooks" in text:
            print(f"{BAD}config.toml 里也有 [hooks] 表 —— 与 hooks.json 同层会被合并并告警，建议只留一种")


def check_state_and_log() -> None:
    section("4. 运行痕迹")
    print(f"{INFO}状态目录 = {APP_DIR}")
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            age = time.time() - STATE_FILE.stat().st_mtime
            print(f"{OK}state.json 存在，{age:.0f} 秒前更新过")
            print(f"{INFO}generation = {state.get('generation')}, sessions = {len(state.get('sessions', {}))}")
            for key, item in list(state.get("sessions", {}).items())[:5]:
                print(f"{INFO}  {key} -> status={item.get('status')} event={item.get('event')}")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{BAD}state.json 读取失败：{exc}")
    else:
        print(f"{BAD}state.json 不存在 —— hook 从来没被真正执行过一次")

    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"{INFO}日志 {len(lines)} 行，最后 10 行：")
        for line in lines[-10:]:
            print(f"        {line}")
    else:
        print(f"{INFO}没有日志文件（只有 HID 写入失败才会写日志，没有不一定是坏事）")


def simulate_hook() -> None:
    section("5. 模拟一次 hook 调用（这一步会真的改灯）")
    payload = json.dumps(
        {"session_id": "diagnose-probe", "hook_event_name": "PreToolUse", "cwd": str(ROOT)}
    )
    before = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0.0
    cmd = [sys.executable, str(CONTROLLER), "hook", "claude", "PreToolUse"]
    print(f"{INFO}执行：{' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, input=payload, capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        print(f"{BAD}超时 —— hook 进程卡住了（很可能卡在读 stdin）")
        return
    print(f"{INFO}exit={result.returncode}")
    if result.stdout.strip():
        print(f"{INFO}stdout: {result.stdout.strip()[:300]}")
    if result.stderr.strip():
        print(f"{BAD}stderr: {result.stderr.strip()[:600]}")
    if result.returncode != 0:
        print(f"{BAD}hook 脚本本身就跑不通，先修这个")
        return

    time.sleep(2.5)
    if not STATE_FILE.exists():
        print(f"{BAD}state.json 仍未生成 —— record_event 没写成功")
        return
    after = STATE_FILE.stat().st_mtime
    if after > before:
        print(f"{OK}state.json 已更新 —— hook -> 状态写入 这一环是通的")
    else:
        print(f"{BAD}state.json 没变化")

    running = _daemon_running()
    if running is None:
        print(f"{INFO}无法枚举进程，跳过 daemon 检查")
    elif running:
        print(f"{OK}后台 daemon 进程已拉起 —— 灯这会儿应该在动")
    else:
        print(f"{BAD}daemon 没起来 —— 状态写进去了但没人去刷灯，问题在 _start_daemon")

    print()
    print(f"{INFO}5 秒后恢复基线灯效…")
    time.sleep(5)
    subprocess.run(
        [sys.executable, str(CONTROLLER), "hook", "claude", "SessionEnd"],
        input=json.dumps({"session_id": "diagnose-probe"}),
        capture_output=True,
        text=True,
    )
    subprocess.run([sys.executable, str(CONTROLLER), "restore"], capture_output=True)
    print(f"{OK}已恢复")


def _daemon_running() -> bool | None:
    try:
        out = subprocess.run(
            ["wmic", "process", "get", "CommandLine"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        try:
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=25,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
    return "_daemon" in (out or "")


def main() -> int:
    print("K500-M81 灯效 hook 诊断")
    check_env()
    codex = check_settings(CODEX_SETTINGS, "Codex")
    claude = check_settings(CLAUDE_SETTINGS, "Claude Code")
    check_codex_trust()
    check_state_and_log()
    simulate_hook()

    section("结论速览")
    if not codex and not claude:
        print(f"{BAD}两边都没装 hook —— 先跑 python .\\install_hooks.py")
    elif not codex:
        print(f"{BAD}Codex 侧没装上")
    elif not claude:
        print(f"{BAD}Claude 侧没装上")
    else:
        print(f"{OK}两边配置文件里都有 hook")
        print(f"{INFO}若配置都在、模拟调用也通，那 99% 是 Codex 的 /hooks 信任没点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
