# 机械键盘 AI 任务状态灯

把 Codex / Claude 的任务状态映射成键盘灯效，让你不用盯着屏幕也知道 AI 在干什么。

| 状态 | 灯效 |
| --- | --- |
| Codex 执行中 | 蓝色跑马灯 |
| Claude 执行中 | 橙色跑马灯 |
| 两边同时执行 | 蓝橙交替（默认 5 秒，见 `BOTH_ALTERNATE_SECONDS`）|
| 任务成功 | 绿色常亮 10 秒 |
| 等待你确认 | 紫色呼吸灯 |
| 会话失败 | 红色急闪 10 秒 |
| 空闲 | 青绿色单点（RGB 0, 255, 170） |

**红灯只在会话真正失败时亮**（`StopFailure`、错误类通知）。工具调用返回非零退出码
**不算失败**——`grep` 没搜到结果退出码就是 1，这在正常干活中太常见，点红灯会让红灯
失去意义。你手动按停止打断也不算失败，走绿灯收尾。

> ⚠️ **灯效指令是设备专属的。**本仓库内置的 7 组 HID 指令抓自一台
> **机械师 K500-M81（固件 V1_13_00）**。换任何其他型号都需要
> [自己抓包并加一份设备配置](#换设备加一份设备配置)，否则灯不会有任何反应。
>
> 换句话说：**如果你不是这个型号，下载下来是不会亮的。**这不是 bug。
> 加配置不用改代码，但需要你会用命令行、能装 Python 包。

## 安全边界

`send_packet()` 里有一道硬性闸门，只放行已验证的 `0x07` 灯光指令：

```python
if packet[0] != 0x01 or packet[1] != 0x07 or packet[2:6] != b"\x00\x00\x00\x0e":
    raise ValueError("Refusing to send a non-lighting HID command")
```

固件更新、按键映射、磁轴行程等任何指令都发不出去。本仓库不包含也不实现这些命令。
官方驱动**不需要**关闭；提示会临时覆盖当前光效，结束后恢复到校准时的单点模式。

## 要求

- Windows（用到 `setupapi` / `hid` / `kernel32`）
- Python 3.10+
- 无第三方依赖（抓包工具除外，它需要 `frida`）

## 快速开始

```powershell
python .\keyboard_lights.py devices          # 列出可用的键盘配置
python .\keyboard_lights.py probe            # 只检测设备，不改灯
python .\keyboard_lights.py show claude --seconds 3
python .\install_hooks.py                    # 安装 Hook（只追加事件）
python .\keyboard_lights.py service install  # 注册登录自启
python .\keyboard_lights.py service start    # 立即启动统一灯效服务
python .\keyboard_lights.py simulate claude  # 完整重放一次会话
```

## 架构：统一灯效服务

```text
Codex Hook  ─┐
             ├─ 追加事件 ─> events.jsonl ─> 唯一 Windows 灯效服务 ─> HID 键盘
Claude Hook ─┘                          │
                                        ├─ 内存状态机
                                        ├─ service-state.json（只读快照，诊断用）
                                        └─ service.log
```

Hook 进程只做一件事：把事件标准化成一行 JSON 追加到
`%LOCALAPPDATA%\MachenikeTaskLights\events.jsonl`，然后退出。它**不写 HID、
不启动任何后台进程、不读共享状态做决策**。

所有决策都在一个常驻的灯效服务里：它持有唯一的 HID 写入权和一个 Windows
命名互斥量（第二个实例启动即退出），在内存里维护全部会话状态，按优先级
`error > approval > done > working > baseline` 合成唯一灯效。绿色/红色 10 秒
到期后重新计算全局状态并**重发**恢复包——不是无条件回基线。

**为什么要这样**：Codex 和 Claude 的桌面应用会把 Hook 跑在各自的沙箱里，同一
路径的可变状态文件在两个沙箱里是两份私有副本（实测同一 `state.json` 两侧
inode 不同），任何依赖共享可变文件做决策的方案都会互相覆盖。唯一实测能穿透
两个沙箱的写入模式是**对已存在文件的追加**，因此事件用追加日志传递，状态机
放进沙箱之外的常驻服务。

其他规则：

- 主会话键是 `(source, session_id)`，子代理另有独立键；`SubagentStop` 和
  `TaskCompleted` 都绝不会结束主会话或点亮整会话绿灯。
- 一个会话结束**只**清除它自己（及其子代理），同来源的其他任务不受影响。
- `working` 状态带 30 分钟租约，由活动事件（每次工具调用）续期；agent 崩溃
  不会留下永久跑马灯。
- 重复 `event_id` 只处理一次；损坏的 JSON 行记日志后跳过；服务重启时重放
  事件日志，恢复所有未过期的会话。
- 事件行不含提示词、工具参数、回答正文；`cwd` 只记哈希。

## 三种接入方式

不同的 AI 客户端接入路径不同：

| 客户端 | 方式 | 状态 |
| --- | --- | --- |
| **Codex CLI** | 官方 Hook | 需在 `/hooks` 里点信任 |
| **Claude Code**（终端版） | 官方 Hook | 装完新开会话即生效 |
| **桌面版 Code 标签页** | 官方 Hook | 与终端版共用同一份配置，无需额外操作 |
| **Cowork**（Claude 桌面版） | — | **无法支持**，见下 |

### Codex / Claude Code：官方 Hook

```powershell
python .\install_hooks.py
python .\install_hooks.py --uninstall   # 移除
```

安装器只追加自己的 Hook，不会覆盖已有的其他 Hook，改动前自动备份原配置。

**Codex 装完还有一步**：非托管 Hook 写进配置后默认「待审核」，必须在 CLI 里执行
`/hooks` 逐条 review 并点「信任」才会真正运行。Hook 定义的哈希一变（重装、改命令、
改超时）信任即作废，需要重新审核。

**Claude Code 注意**：Hook 在会话启动时加载，给已开着的会话装 Hook 不生效，需新开会话。

### 桌面版 Code 标签页

**开箱即用，不需要任何额外配置。**实测（2026-08-28）：只要 Windows 侧的
`%USERPROFILE%\.claude\settings.json` 里装好了 Hook，Code 标签页的会话就会正常触发。

启动 Code 时可能弹出 WSL 相关提示，但这**不影响** Hook——它读的仍是 Windows 侧的配置。
不必为此在 WSL 里另装一份。

### `--wsl` 模式：仅在 Hook 确实跑在 WSL 里时才需要

> ⚠️ **此模式未经端到端实测。**开发这套东西的机器上没有安装任何 WSL 发行版，
> 因此下面的路径推导逻辑虽经代码审查，但从未真正跑通过一次。
> 如果你在真实 WSL 环境下用了它，欢迎开 issue 反馈结果。

如果某种场景下 Claude Code 确实运行在 WSL 内（读 WSL 的 `~/.claude/settings.json`），
用这个模式安装：

```bash
# 在 WSL 里，cd 到本仓库（形如 /mnt/c/...）
python3 install_hooks.py --wsl
python3 install_hooks.py --wsl --uninstall     # 移除
python3 install_hooks.py --wsl --windows-python /mnt/c/Python314/python.exe   # 手动指定
```

**这个模式为什么特殊**：Hook 在 Linux 里触发，但必须以 Windows 进程执行才能访问 HID
设备。所以生成的 Hook 里两个路径形态**故意不同**：

```json
{
  "command": "/mnt/c/Python314/python.exe",
  "args": ["C:\\...\\keyboard_lights.py", "hook", "claude", "PreToolUse"]
}
```

`command` 由 Linux 执行，必须是 WSL interop 能识别的 `/mnt/c/...`；`args` 里的内容会原样
交给那个 Windows 进程，所以脚本路径必须是原生 `C:\...`——写成 `/mnt/c/...` 的话
`python.exe` 会报 "No such file or directory"。

WSL 有 Windows 互操作能力，这正是它与下面 Cowork 沙箱的关键区别。

### Cowork 为什么不支持

**三重阻断，全部实测确认，目前无解**（均为上游行为，非本项目问题）：

1. **Hook 不触发**。Cowork 静默忽略 `~/.claude/settings.json`、托管设置和环境变量覆盖
   （anthropics/claude-code [#40495](https://github.com/anthropics/claude-code/issues/40495)、
   [#63360](https://github.com/anthropics/claude-code/issues/63360)）。实测：新会话中调用工具，
   状态文件的 `generation` 与日志均无任何变化。
2. **配置根只读**。Cowork 的配置根是每会话独立的沙箱目录，`touch` 直接 Permission denied，
   因此「自己重定向配置」也走不通。
3. **执行环境是隔离的 Linux 容器**。即使前两条被绕过，Hook 命令也会在容器内执行。
   实测该容器运行在标准 Ubuntu 内核（`6.8.0-generic`，无 Microsoft 标识）、cgroup 为
   `/coworkd/...`，**不具备 WSL 的 Windows 互操作能力**：没有 `/mnt/c`、没有
   `WSLInterop`，`ctypes.WinDLL` 也不存在。因此上面 WSL 那套 interop 方案在这里用不了。

绕开 Hook 去监听 transcript 同样不可行：**Cowork 的 transcript 只存在于 Linux 沙箱内**，
Windows 文件系统上没有对应文件。实测搜遍 `%LOCALAPPDATA%\Claude*`、`%APPDATA%\Claude`、
`~\.claude`、`~\Claude`，只能找到终端版 Claude Code 的会话记录，没有任何 Cowork 记录。

**结论：要让 Claude 侧亮灯，请使用终端版 Claude Code。**

### `watch_cowork.py`：无 Hook 场景的备选

监听器本身可用，只是它能看到的是**终端版 Claude Code** 写在
`~\.claude\projects\` 下的 transcript，而非 Cowork：

```powershell
python .\watch_cowork.py             # 常驻，Ctrl+C 退出
python .\watch_cowork.py --dry-run   # 只打印事件，不点灯
python .\watch_cowork.py --replay <文件> --dry-run   # 离线验证映射逻辑
```

映射规则：用户发消息 → 跑马灯；`stop_reason: end_turn` → 绿灯；
`tool_use` 超过 6 秒没等到 `tool_result` → 紫灯（多半在等你授权）；
`toolDenialKind` → 红灯。

对终端版 Claude Code 而言，**官方 Hook 是更好的选择**（事件精确、无需轮询、无格式风险）。
这个监听器只在你不想或不能装 Hook 时才有意义。

> ⚠️ transcript 是内部格式，没有兼容性承诺。失效时改
> `watch_cowork.py` 里的 `SessionState.observe()`。

## 服务管理与排查

```powershell
python .\keyboard_lights.py service status      # 服务 PID、活动会话、当前灯效、日志尾部
python .\keyboard_lights.py service start       # 启动（优先走计划任务/启动项，沙箱外）
python .\keyboard_lights.py service stop        # 停止服务（按 PID + 完整命令行精确核对）
python .\keyboard_lights.py service install     # 注册登录自启（计划任务被拒时写启动文件夹）
python .\keyboard_lights.py service uninstall   # 移除自启并停止服务
python .\keyboard_lights.py stop                # 停服务 + 清理旧版 daemon + 恢复基线
python .\keyboard_lights.py simulate claude     # 走真实事件管道重放一次会话
python .\keyboard_lights.py simulate claude --inline   # 服务循环跑前台，打印每次 HID 写入
python .\diagnose.py                            # 检查 Hook / 服务 / 事件追加是否都通
python -m unittest test_light_service           # 状态机单元测试（不碰键盘）
```

`simulate` 不需要 AI 参与、不消耗额度，额度用尽时也能验证。
`--inline` 用来区分**状态机的问题**和**服务生命周期的问题**。

数据目录：`%LOCALAPPDATA%\MachenikeTaskLights\`
（`events.jsonl` 事件日志、`service.log` 服务日志、`service-state.json` 只读快照）

### 两个安装时会踩的坑

**Codex 的 hook 命令必须以 `&` 开头。** Codex 用 PowerShell 执行 shell 形式的 hook，
而 PowerShell 里一条以引号路径开头的命令只是个字符串表达式——不加 `&` 调用运算符，
它会抛解析错误、**不启动任何进程**，但整体仍然返回成功。于是 hook 装好了、信任也点了，
却一次都没执行过，而且没有任何地方会报错。安装器已经会自动加上，手写配置时别漏：

```
& "C:/Python314/python.exe" "C:/.../keyboard_lights.py" hook codex PreToolUse
```

**hook payload 必须按 UTF-8 解码，不能用控制台代码页。** Windows 给管道 stdin 的默认
编码是系统 ANSI 代码页（中文系统是 gbk），而 hook payload 是 UTF-8 JSON。仓库路径带
非 ASCII 字符时，UTF-8 字节按 gbk 解出来会撞出 `\`，JSON 转义当场崩掉，整个 payload
解析失败、`session_id` 丢失——症状是**只有装在中文路径下的项目会乱**，而且要多个项目
同时跑才看得出来。`light_client.py` 现在读二进制再显式按 UTF-8 解码。

灯不动时按顺序看三样：`service status` 显示服务在不在跑；`events.jsonl`
最近有没有追加（没有 = Hook 没触发，多半是 Codex `/hooks` 信任没点）；
`service.log` 里 HID 写入有没有报错。

## 换设备：加一份设备配置

设备的 VID/PID 与 7 组灯效指令都放在 `devices/*.json` 里，**加一款键盘是加一个文件，
不用改 Python**。程序启动时会扫描 `devices/`，按已插入的 VID/PID 自动选择配置。

```powershell
python .\keyboard_lights.py devices                     # 看有哪些配置、当前用哪个
python .\keyboard_lights.py --device k500-m81 probe     # 手动指定
$env:KEYBOARD_LIGHTS_DEVICE = "k500-m81"                 # 或用环境变量
```

### 抓自己键盘的指令

1. 装 `frida`：`pip install frida`
2. 找到官方驱动进程的 PID，挂上抓包脚本：

   ```powershell
   python .\tools\capture_hid_writes.py --pid <驱动PID> --seconds 120
   ```

3. 在官方驱动里逐个切换灯效（跑马灯、常亮、呼吸……），记下每种对应的包
4. 照着 `devices/k500-m81.json` 的格式新建一个文件：

```json
{
  "name": "你的键盘型号",
  "vid": "0x0416",
  "pid": "0x7372",
  "interface_marker": "&mi_02#",
  "report_length": 64,
  "lighting_prefix": "01 07 00 00 00 0E",
  "packets": {
    "baseline":       ["<空闲时的包体>", "<尾部校验字节>"],
    "codex_working":  ["...", "..."],
    "claude_working": ["...", "..."],
    "success":        ["...", "..."],
    "permission":     ["...", "..."],
    "error":          ["...", "..."],
    "off":            ["...", "..."]
  }
}
```

每个包写成 `[包体, 尾部]` 两段，中间的驱动填充字节由程序按 `report_length` 补零。

> **`lighting_prefix` 是安全闸门，不是装饰。**加载配置时会逐个检查 7 组包是否都以它开头，
> 发送前再查一次。这样即使有人往 `devices/` 里塞一个把固件刷写指令伪装成灯效的配置，
> 也会在加载阶段直接被拒绝。填你的灯光指令共有的前缀，**不要**为了让某个包通过而缩短它。

抓包脚本只观察写入、不修改任何数据。欢迎把你抓到的型号提 PR 回来。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `keyboard_lights.py` | HID 写入、设备配置、CLI、服务管理命令 |
| `light_client.py` | Hook 客户端：把事件追加到 `events.jsonl` 后立即退出 |
| `light_service.py` | 统一灯效服务：内存状态机 + 事件重放/尾随 + 渲染 |
| `test_light_service.py` | 状态机与事件管道的单元测试（mock HID） |
| `install_hooks.py` | 合并/移除 Codex 与 Claude Code 的 Hook 配置 |
| `watch_cowork.py` | Transcript 监听器（无 Hook 场景的备选，同样只追加事件） |
| `diagnose.py` | 端到端诊断 |
| `tools/capture_hid_writes.py` | 从官方驱动抓灯光指令 |
| `devices/*.json` | 各型号的 VID/PID 与灯效指令（加键盘=加文件） |

## 许可

MIT，见 [LICENSE](LICENSE)。

本项目与机械师（Machenike）、OpenAI、Anthropic 均无关联。
HID 指令由使用者自行从自己设备的官方驱动中抓取。
