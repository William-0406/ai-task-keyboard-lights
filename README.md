# 机械键盘 AI 任务状态灯

把 Codex / Claude 的任务状态映射成键盘灯效，让你不用盯着屏幕也知道 AI 在干什么。

| 状态 | 灯效 |
| --- | --- |
| Codex 执行中 | 蓝色跑马灯 |
| Claude 执行中 | 橙色跑马灯 |
| 两边同时执行 | 蓝橙每 2 秒交替 |
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
python .\keyboard_lights.py simulate claude  # 完整重放一次会话
python .\install_hooks.py                    # 安装 Hook
```

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

## 排查

```powershell
python .\keyboard_lights.py status              # daemon 心跳、当前状态、日志尾部
python .\keyboard_lights.py stop                # 清状态 + 强杀残留 daemon + 恢复基线
python .\keyboard_lights.py simulate claude     # 走真实 Hook 路径重放一次会话
python .\keyboard_lights.py simulate claude --inline   # 灯光循环跑前台，打印每次 HID 写入
python .\diagnose.py                            # 检查 Hook 装没装、模拟一次调用
```

`simulate` 不需要 AI 参与、不消耗额度，额度用尽时也能验证。
`--inline` 与普通模式的唯一差别是「灯光循环在不在独立进程里」，
用来区分**状态机/灯效指令的问题**和**分离进程的问题**。

日志位置：`%LOCALAPPDATA%\MachenikeTaskLights\keyboard-lights.log`

### 已知失效模式：daemon 卡死

灯光由一个后台 daemon 轮询状态文件驱动，单实例靠命名互斥量保证。

早期版本只检查互斥量有没有被占用，**不检查持有者是否还活着**。一旦某个 daemon 卡死
（对 HID 设备的同步 `WriteFile` 没有超时，设备不收就会永久阻塞），它会一直攥着互斥量，
此后每个新 daemon 启动即自杀——**灯从此永久不亮，且没有任何报错**。

现在有两道防线：

1. daemon 每 2 秒写一次心跳。互斥量被占但心跳超过 8 秒未更新时，新 daemon 判定持有者
   已死并接管。
2. 没有活着的 daemon 时，Hook 进程自己直接发灯效指令，daemon 机制整体瘫痪也不影响提示。

`status` 会把卡死的 daemon 明确标出来，`stop` 可以强杀。

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
| `keyboard_lights.py` | 主程序：HID 写入、状态机、后台 daemon、CLI |
| `install_hooks.py` | 合并/移除 Codex 与 Claude Code 的 Hook 配置 |
| `watch_cowork.py` | Transcript 监听器（无 Hook 场景的备选） |
| `diagnose.py` | 端到端诊断 |
| `tools/capture_hid_writes.py` | 从官方驱动抓灯光指令 |
| `devices/*.json` | 各型号的 VID/PID 与灯效指令（加键盘=加文件） |

## 许可

MIT，见 [LICENSE](LICENSE)。

本项目与机械师（Machenike）、OpenAI、Anthropic 均无关联。
HID 指令由使用者自行从自己设备的官方驱动中抓取。
