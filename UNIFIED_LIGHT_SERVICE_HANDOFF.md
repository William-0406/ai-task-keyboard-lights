# 统一键盘灯效服务：实施交接

## 任务目标

把 Codex 与 Claude 的键盘提示统一交给一个常驻 Windows 灯效服务管理。Hook 只上报事件，不再直接写 HID、不再启动各自的 daemon，也不再读写用于决策的共享 `state.json`。

最终行为：

| 状态 | 灯效 |
|---|---|
| Codex 正在工作 | 纯蓝色跑马灯 |
| Claude 正在工作 | 纯橙色跑马灯 |
| 两者同时工作 | 蓝色与橙色按 2 秒交替 |
| 主任务成功结束 | 亮绿色常亮 10 秒 |
| 等待用户授权或输入 | 紫色呼吸灯 |
| API、连接或回答失败 | 红色急速闪烁 10 秒 |
| 没有活动任务 | 青绿色单点模式，RGB `(0, 255, 170)` |

必须继续使用 `devices/k500-m81.json` 中已经校准的 HID 数据包，尤其是关闭“全彩”后的纯蓝、纯橙跑马灯数据。不要重新推测或生成灯效包。

## 已确认的故障事实

2026-08-31 的实测事件顺序：

1. `10:08:21`，Claude 的 `UserPromptSubmit` 正常到达并发送 `CLAUDE_WORKING`。
2. `10:10:03`，另一个 Codex 会话触发 `SessionEnd`，其 daemon 读不到 Claude 的活动状态，发送了 `BASELINE`，覆盖橙色。
3. `10:11:42`，Codex daemon 发送 `CODEX_WORKING`。
4. `10:12:16`，Claude `Stop` 发送绿色 `SUCCESS`。
5. `10:12:30`，Claude daemon 恢复 `BASELINE`，覆盖仍在运行的 Codex 蓝色。
6. Codex daemon 内部仍缓存“蓝色已经发送”，因此没有补发蓝色。

日志里的同一路径出现了两个不同的文件标识：

- Claude 侧 `state.json` inode：`86131342873830740`
- Codex 侧 `state.json` inode：`32088147345065688`
- 追加式 `keyboard-lights.log` 在两侧始终是同一个 inode：`7599824371648993`

这说明应用沙箱会让同一路径的可变状态文件产生隔离视图。把写入方式从替换文件改成原地写入仍不能保证 Codex、Claude 和子进程看到同一份状态。曾同时存在 PID `20508` 与 `12812` 两个 `_daemon`，进一步证明现有“单实例 + 文件心跳”机制无法跨两个沙箱可靠工作。

## 目标架构

```text
Codex Hook  ─┐
             ├─ 追加事件 ─> events.jsonl ─> 唯一 Windows 灯效服务 ─> HID 键盘
Claude Hook ─┘                         │
                                      ├─ 内存状态机
                                      ├─ service-state.json（只用于诊断/恢复）
                                      └─ service.log
```

### 1. Hook 客户端

Hook 进程只做以下工作：

1. 从 stdin 读取 Hook JSON。
2. 提取并标准化事件字段。
3. 向 `%LOCALAPPDATA%\MachenikeTaskLights\events.jsonl` 追加一行 JSON。
4. 立即退出。

Hook 客户端不得：

- 调用 `send_packet()`；
- 启动、停止或探测 daemon；
- 根据 `state.json` 计算当前灯效；
- 等待绿色或红色提示结束；
- 清理其他会话。

单条事件建议结构：

```json
{
  "schema": 1,
  "event_id": "uuid",
  "recorded_at": 1788142421.853,
  "source": "codex",
  "event": "PreToolUse",
  "session_id": "01a046bc-...",
  "turn_id": "optional",
  "agent_id": "optional",
  "notification_type": "optional",
  "error": "optional"
}
```

写入要求：

- UTF-8，一条事件一行；
- 每个事件生成唯一 `event_id`；
- 单行保持在 4 KiB 内，不记录提示词、工具参数、回答正文或密钥；
- 使用跨进程命名互斥量保护单次追加；
- 一次 `write()` 写完整行并立即 flush；
- 追加失败要写 Windows Event Log 或 stderr，但不能阻断 Codex/Claude 主任务。

追加式日志已经被实测为 Codex 与 Claude 两侧都能看到。实现后仍须用自动化测试和一次真实双应用测试重新验证这一性质。

### 2. 唯一 Windows 灯效服务

灯效服务必须从普通 Windows PowerShell、登录启动项或计划任务启动，使其生命周期独立于 Codex 与 Claude 沙箱。Hook 不负责拉起服务。

建议命令：

```powershell
python .\keyboard_lights.py service install
python .\keyboard_lights.py service start
python .\keyboard_lights.py service stop
python .\keyboard_lights.py service status
python .\keyboard_lights.py service uninstall
```

服务职责：

- 持有唯一的 HID 写入权；
- 持续读取 `events.jsonl` 新增记录；
- 在内存中维护所有会话状态；
- 根据全局状态决定唯一灯效；
- 执行绿色、红色效果的 10 秒计时；
- 在高优先级提示到期后，立即根据仍存活的任务恢复蓝色、橙色或二者交替；
- 输出可诊断的 `service.log` 和只读状态快照。

单实例要求：

- 由服务进程持有真正的 Windows 命名互斥量；
- 第二个服务进程发现互斥量已存在时必须退出；
- 不再通过沙箱内的 `state.json` 心跳决定是否接管；
- `status` 应同时显示服务 PID、启动时间、最近处理的 `event_id`、当前活动会话和实际决定的灯效。

### 3. 状态机语义

主会话键使用 `(source, session_id)`。子代理如需独立追踪，键使用 `(source, session_id, agent_id)`，不得覆盖主会话。

事件映射：

| 事件 | 状态处理 |
|---|---|
| `SessionStart` | 注册会话但保持空闲，不点亮工作灯 |
| `UserPromptSubmit` | 主会话进入 `working` |
| `PreToolUse` | 对应会话进入或续租 `working` |
| `PostToolUse` | 对应会话保持或恢复 `working` |
| `PostToolUseFailure` | 仍视为 `working`，工具失败不等于整次回答失败 |
| `PermissionRequest` | 对应会话进入 `approval` |
| `Elicitation` | 对应会话进入 `approval` |
| `PermissionDenied` | 清除 `approval`；若主任务仍在继续则回到 `working` |
| `Stop` | 仅主会话进入 `done`，持续 10 秒 |
| `StopFailure` | 仅主会话进入 `error`，持续 10 秒 |
| `SessionEnd` | 只移除精确匹配的主会话及其子代理 |
| `SubagentStart` | 创建或更新对应子代理，不结束主会话 |
| `SubagentStop` | 只结束对应子代理；绝不能把主会话标为完成 |
| `TaskCompleted` | 只更新对应任务/子任务；绝不能单独触发整次会话绿色完成灯 |
| 普通 `Notification` | 仅在明确表示权限、等待输入或错误时改变状态 |

禁止“某个会话结束时清除同来源所有 working 会话”。同时运行两个 Codex 任务或两个 Claude 任务时，一个任务结束不能影响另一个任务。

建议优先级：

```text
error > approval > done > working > baseline
```

高优先级提示到期后必须重新计算全局状态，而不是无条件恢复 baseline。

工作状态可保留租约防止异常退出造成永久跑马灯。租约应由活动事件续期，默认建议 30 分钟，并由 `Stop`、`StopFailure`、`SessionEnd` 精确结束。租约值应集中配置并在状态命令中显示剩余时间。

### 4. 灯效写入规则

- 只有统一服务可以调用 `send_packet()`。
- 服务的 `last_packet` 只能用于减少本服务自己的重复写入。
- 绿色或红色到期后，即使目标数据包等于高优先级提示开始前的缓存，也必须重新发送恢复后的数据包。
- 错误灯由服务按约 120 ms 在红色与关闭包之间切换，10 秒后重新计算全局状态。
- Codex 与 Claude 同时工作时，服务每 2 秒在蓝色和橙色包之间切换。
- 服务退出前只有在确认没有活动任务时才恢复青绿色单点模式。

## 安装与迁移

1. 保留当前 `devices/k500-m81.json` 与 HID 安全前缀校验。
2. 为 Hook 客户端、事件日志和统一服务增加独立模块，避免在一个函数里混合三种职责。
3. 增加纯状态机单元测试，所有测试使用临时目录并 mock `send_packet()`。
4. 停止所有命令行包含 `keyboard_lights.py _daemon` 的旧进程；终止前按 PID 和完整命令行精确核对。
5. 安装并从普通 PowerShell 启动统一服务。
6. 修改 Codex 与 Claude Hook，使其只执行事件追加客户端。
7. Codex 的 `SessionEnd` Hook timeout 必须不大于 3 秒；当前安装配置里是 5 秒，需要迁移为 3 秒。
8. Claude 增加 `PermissionDenied`，保留 `StopFailure`。
9. 重新打开 Codex 和 Claude，并重新审核/信任发生变化的 Codex Hook。
10. 确认机器上始终只有一个拥有 HID 写入权的服务进程。

安装程序必须继续保留用户已有的其他 Hook，更新过程要幂等，并在改写配置前创建带时间戳的备份。

## 必须通过的测试

### 自动化测试

1. Codex A、Codex B 同时 `working`，A `Stop` 后 B 仍为蓝色。
2. Claude A、Claude B 同时 `working`，A `SessionEnd` 后 B 仍为橙色。
3. Codex 与 Claude 同时运行时按 2 秒交替蓝橙。
4. Claude `Stop` 触发绿色 10 秒，同时 Codex 保持运行；10 秒后无需任何新 Hook，自动恢复蓝色。
5. Codex `Stop` 触发绿色 10 秒，同时 Claude 保持运行；10 秒后自动恢复橙色。
6. `SubagentStop` 不触发主会话绿色，也不停止主会话跑马灯。
7. `TaskCompleted` 不结束仍在运行的主会话。
8. `PermissionRequest` 显示紫色，批准、拒绝或后续工具事件后恢复正确工作灯。
9. `StopFailure` 显示红色急闪 10 秒，之后恢复仍存活的其他任务灯效。
10. 重复 `event_id` 只处理一次。
11. 损坏或不完整 JSON 行被记录并跳过，不导致服务退出。
12. 服务重启后重放事件日志，能恢复尚未过期的活动会话。
13. 两个并发安装或启动请求最终只能留下一个服务实例。

### 真实设备验收

1. 单独运行 Codex，观察纯蓝跑马灯。
2. 单独运行 Claude，观察纯橙跑马灯。
3. 两者同时运行，观察蓝橙交替。
4. 在其中一个任务结束并显示绿色期间，让另一个任务继续运行；绿色结束后确认自动恢复另一个任务的颜色。
5. 触发一次真实权限请求，确认紫色呼吸；处理请求后恢复工作灯。
6. 用模拟 `StopFailure` 验证红色急闪，再确认恢复逻辑。
7. 关闭两个应用后确认最终回到青绿色单点。
8. 检查任务管理器和 `service status`，确认只有一个灯效服务。

## 完成定义

交付前需同时满足：

- Codex 与 Claude Hook 不再直接写 HID 或启动 daemon；
- 只有一个独立 Windows 服务写键盘；
- 不再依赖跨沙箱共享的可变 `state.json` 做决策；
- 上述自动化测试全部通过；
- 上述真实设备验收全部通过；
- `README.md`、安装/卸载命令和诊断命令已同步更新；
- 日志不包含提示词、回答正文、工具参数或其他敏感内容。

