<h1 align="center">Aki</h1>

<p align="center">
  <strong>Local cyber lifeform · Unix socket gateway</strong><br/>
  <strong>Aki / 秋 / 小秋 / 秋（aki）</strong>
</p>

<p align="center">
  A local-first runtime for a persistent Letta agent: Unix-socket JSON-RPC, approvals, MemFS, MQTT, and a KDE Plasma widget.
</p>

<p align="center">
  Python standard library · JSON-RPC over NDJSON · Letta stream-json · KDE Plasma 6
</p>

---

## 仓库描述

> A local-first gateway for a persistent Aki agent: Unix socket, JSON-RPC, Letta, MQTT, and a KDE Plasma widget.

Aki 是一个本地优先的持久化 Agent 运行时。`akid` 只监听 Unix socket，监督一个长驻的
`letta --input-format stream-json --output-format stream-json` 子进程，把对话、工具审批、
记忆、MQTT 频道和 KDE Plasma 小组件连接成一套可以长期运行的本地 Agent 系统。

这个仓库关心的不是“再做一个聊天 CLI”，而是几个更具体的问题：同一个 Aki 如何在多次
会话之间保持连续身份；危险操作如何由发起者批准；多个本地客户端如何共享状态而不共享
实时事件；以及人格和记忆如何被版本化、备份和恢复。

## 设计取向

- **本地优先，不暴露 TCP。** 对外只有一个 Unix socket；目录 `0700`、socket `0600`，
  监听后校验 `SO_PEERCRED` 与当前用户 UID。TCP 不是可选项。
- **一个长驻后端，一个活跃 turn。** `akid` 监督一个 Letta stream-json 子进程；多个客户端
  可以同时连接，但同一时间只有一个 turn 在跑，后续消息 FIFO 排队。
- **审批属于发起者。** 危险工具只向该 turn 的发起客户端请求批准；超时默认拒绝该工具，
  但 turn 继续。MQTT 频道发起的 turn 只能由频道里的原始发送者审批。
- **人格与记忆是文件。** 核心人格在 MemFS 的 Markdown 文件里；`userdata/` 保存非隐私
  模板，`scripts/sync-persona.py` 渲染 `{{USERNAME}}` 并写回 MemFS。
- **工具结果是数据，不是图片垃圾桶。** 截图和摄像头只返回路径与元数据，图片内容不会
  自动进入模型上下文；模型需要看时自行 `Read`/`ViewImage`。
- **默认最小权限。** Letta 及其后代进程跑在 bubblewrap 里，只能写 `/tmp`、
  `~/.local/share/tmp`、Letta 自己的状态目录，以及用户显式注册的目录。

## 功能

- **Unix socket JSON-RPC 网关。** NDJSON，每行一个 JSON 对象，单行上限 1 MiB；
  支持 `aki.hello`、`aki.chat.*`、`aki.approval.*`、`aki.memory.*`、`aki.fs.writable.*`、
  `aki.mqtt.*`、`aki.username.*`、`aki.conversation.reset`。
- **长驻 Letta 监督。** 等待 `system/init` 后 ready；异常退出按 1/2/4/8/16/30 秒退避重启；
  当前 turn 标记失败，队列保留。
- **多客户端。** 状态、历史和只读记忆共享；实时事件只发给该 turn 的发起连接。
- **审批工作流。** `Read`、`ReadLSP`、`Grep`、`Glob`、`LS`、`Memory`、`ViewImage` 和
  `mcp__tavily__*` 自动放行；`Bash` 在 sandbox 就绪时也默认放行。`Write`/`Edit`/
  `ApplyPatch` 和未知工具需要发起客户端批准，默认 60 秒超时拒绝。
- **tokifactor MQTT 频道。** 加密文本/图片、分片消息、头像发布、频道内审批，以及
  `akid.v1` 结构化事件。
- **KDE Plasma 6 widget。** 历史分页、流式输出、Markdown、工具调用卡片、GUI 审批卡、
  停止/排队状态和系统通知。
- **可写目录沙盒。** 手动通过 `akictl allow` 注册，随时可吊销；注册/吊销会重启后端。
- **人格与 userdata。** 人格模板、称呼占位符、MemFS 渲染、备份和 git 提交。
- **工具集。** tokifactor 图片发送、摄像头拍照、KDE/grim 截图，以及 `akictl` 管理命令。
- **桌面实况上下文。** 每个 turn 在系统提示词中原地刷新唯一的 `<aki-live-context>` 块，
  包含当前时间、焦点窗口、CPU top 5、最近 20 条 zsh 命令和截图路径；不写入消息历史。

---

## 快速开始

```bash
python3 install-daemon.py --dry-run          # 预览 unit，不落盘
python3 install-daemon.py                    # 安装并启动 akid.service
python3 install-daemon.py --status
python3 install-daemon.py --restart
python3 install-daemon.py --uninstall        # 不删除 MemFS/历史
```

安装时会把 `akictl` 链接到 `~/.local/bin/akictl`（指向本仓库的 `bin/akictl`）。
如果该路径已存在且不是本仓库的链接，安装会报错而不会覆盖；`--uninstall`
只会移除指向本仓库的链接。

默认 socket 为 `$XDG_RUNTIME_DIR/aki/akid.sock`。手动运行时：

```bash
AKI_SOCKET=/tmp/aki/akid.sock python3 engine/akid.py
```

### KDE Plasma Widget

```bash
python3 install-widget.py --dry-run   # 预览 unit 和安装路径
python3 install-widget.py             # 安装 widget + 启动 GUI 桥接
python3 install-widget.py --status
python3 install-widget.py --uninstall # 保留 akid、MemFS 和聊天历史
```

安装后右键底部面板 →“添加组件”→ 搜索 `Aki`。widget 平时显示 Aki 头像，
点击后在面板弹窗中聊天；当前实现只使用现有 `default` 会话，输入只支持文本，
工具审批是单次 allow/deny。

架构：

```text
Plasma QML widget
        │  WebSocket (JSON-RPC)
        ▼
akid-gui.service (127.0.0.1:8765)
        │  NDJSON
        ▼
akid Unix socket
```

QML 不能直接连接 Unix socket，因此 `akid-gui.service` 做透明代理：一个
WebSocket 文本帧对应一个 JSON-RPC 对象，桥接只在转发到 akid 时补换行。
桥接不解析、不记录消息正文。

- token 位于 `~/.local/share/aki/gui-token`（`0600`），连接 URL 必须带
  `?token=`；桥接只绑定 `127.0.0.1`。
- `~/.local/share/aki/gui-bridge.json`（`0600`）记录 host、port 和 URL，方便
  未来其他本地 GUI/接口复用。
- widget 支持历史分页、流式输出、Markdown 与 fenced code block、工具调用卡片、
  GUI 审批卡、停止/排队状态、错误提示和 KDE 系统通知。
- 聊天框关闭时收到审批请求会发送 High urgency 的持久通知，并把面板图标设为
  需要注意状态；点击通知会打开聊天框。
- 断开桥接或 akid 时会显示错误并按退避重连。

---

## 客户端

```bash
akictl -p "你好"                  # 单次流式输入
akictl -p "你好" --json           # 原始 NDJSON 服务端输出
akictl                            # 流式 REPL
bin/aki -p "你好"                 # 兼容别名
```

REPL 命令：`/status`、`/health`、`/history [n]`、`/allow <dir>`、
`/disallow <dir>`、`/allowlist`、`/memory tree|read|search|tokens|status`、`/username`、
`/set-username <name>`、`/recompile`、`/reset`、`/quit`。
危险工具会显示工具名与参数并询问 `y/N`；非交互 stdin 默认拒绝。`Ctrl-C` 取消当前 turn。

重置 `default` 会话：

```bash
akictl reset
```

重置会先把旧 transcript 备份到 `~/.letta/backups/aki-default-reset-<timestamp>`，
然后清空 `messages.jsonl`、`in_context_message_ids`、summary 和 last_message_at，
并重启 Letta 后端。活跃 turn 会被中断，队列中的 turn 会被取消。

---

## 人格与 userdata

Aki 的名字是 Aki，中文名是秋、小秋，日语名是秋（aki）。这些称呼都指向同一个身份。

`userdata/` 保存 MemFS 中可公开、非隐私的人格模板与记忆策略：

- `persona.md`
- `memory-policy.md`
- `character/MEMORY.md`
- `character/voice.md`
- `character/relationship.md`
- `character/boundaries.md`
- `character/examples.md`
- `username.txt` — 当前对用户的称呼（默认“Master”）

模板中用 `{{USERNAME}}` 表示称呼。同步脚本会把占位符替换成 `username.txt`
的值，再把渲染后的内容写入 MemFS；模型看到的是最终文本，不需要自己读配置文件。

修改称呼：

```bash
akictl username                 # 查看当前称呼
akictl set-username Master      # 修改、渲染并重编译当前会话
```

也可以手动编辑 `userdata/username.txt`，再运行同步脚本：

```bash
python3 scripts/sync-persona.py --dry-run   # 预览
python3 scripts/sync-persona.py             # 渲染、备份、写入、提交 MemFS
akictl recompile                            # 手动同步后重编译当前会话
```

脚本只接受上述白名单文件，会校验 frontmatter、检查未解析的占位符、备份旧版本到
`~/.letta/backups/userdata-sync-<timestamp>/`，并提交 MemFS git。
`akictl set-username` 会在同步后自动重编译当前会话的 system prompt；手动修改 `userdata` 后运行 `akictl recompile` 也可以立即应用。

`human.md`、`profile/**`、根 `MEMORY.md`、聊天记录和凭据不会同步到 `userdata`。

---

## 可写目录沙盒

akid 用 `bubblewrap` 包裹整个 Letta 子进程。Letta、Bash、文件工具及其所有后代进程都
只能写入：

- `/tmp`
- `~/.local/share/tmp`
- Letta 内部运行状态：`~/.letta`（以及 `LETTA_LOCAL_BACKEND_DIR` 指向的外部目录）
- 用户手动注册的目录

读取不受限制；网络与 MQTT 行为不变。`XDG_CACHE_HOME`、npm/pip/uv cache、
`XDG_RUNTIME_DIR`、`TMPDIR` 会自动重定向到 `~/.local/share/tmp/akid`。
如果 bwrap 不可用，Letta 后端不会启动，akid 以 fail-closed 方式保持 degraded。

可写目录必须通过本地 Unix socket 管理，MQTT 频道无权修改：

```bash
akictl allowlist
akictl allow /path/to/project
akictl disallow /path/to/project
akictl disallow --all
akictl mqtt                       # 查看当前 MQTT 服务器
akictl mqtt set 192.168.3.1:1883  # 持久化并立即重连
akictl username                   # 查看当前称呼
akictl set-username Master        # 修改称呼并重编译当前会话
akictl recompile                  # 从 MemFS 重编译当前会话 prompt
akictl reset                      # 重置 default 会话（先备份）
```

注册表持久化在 `~/.local/share/aki/writable-dirs.json`（目录 `0700`、文件 `0600`）。
注册/吊销会立即重启 Letta 后端；若有活跃 turn，该 turn 会收到
`SANDBOX_POLICY_CHANGED` 和 `turn_end.reason=sandbox_restart`，队列中的其他 turn 保留。

---

## tokifactor MQTT 频道

akid 默认同时连接 tokifactor 公共频道：

- Broker：`192.168.3.1:1883`（MQTT 3.1.1，QoS 1）
- Topic：`tokifactor/text`
- Magic string：`akid-default-channel`

频道消息使用 tokifactor 相同的线格式：`HKDF-SHA256` 派生密钥，
`nonce || AES-GCM-SIV` 密文，Base64 后发布；短消息是 `TextEnvelope` JSON，
长消息是加密的 `TextChunk` 分片。实现位于 `engine/tokifactor.py`、
`engine/mqtt_client.py` 和 `engine/channel.py`。

收到频道消息后，akid 会把它作为新的 Agent turn 处理，并在提示词中要求 Agent
自行判断是否需要回复；不需要回复时 Agent 输出 `[NO_REPLY]`，网关会抑制发布。
需要回复时会发布阶段里程碑：处理状态、assistant 文本阶段、工具开始/结束和最终
Markdown 回复；客户端按 `turnId` 原位更新，不会等整轮结束后才出现第一条回复。
`[NO_REPLY]` 会让客户端移除该 turn 的临时卡片。

频道也是工具审批通道。危险工具触发时，akid 会发布审批提示和 `approval_id`。
在频道中发送固定字符串：

```text
akid-approve              # 同意最早待处理的审批
akid-deny                 # 拒绝最早待处理的审批
akid-approve <approval_id>
akid-deny <approval_id>
```

也可写成 `akid approve|deny [approval_id]`。频道只能审批频道发起的 turn；
审批命令还必须来自原始 MQTT 消息的 sender；本地 turn 仍需本地客户端调用
`aki.approval.respond`。tokifactor 会把这些请求渲染成 Allow/Deny 按钮，
按钮发送的仍是上述加密文本命令。

结构化事件使用兼容的 `akid.v1` TextEnvelope 扩展字段；旧客户端仍会看到
fallback 文本。工具 stdout/stderr、工具参数、reasoning 和 usage 不会发布到频道。

> 安全提示：公共 broker 不可信，magic string 是群组口令。任何知道该 magic
> string 的人都能读取频道、触发 Agent turn，并发送审批命令。不要在频道中
> 传播秘密；如需真实隔离，应换用私有 broker 和不同的 magic string。

---

## 工具

### tokifactor 图片

akid 通过 Letta external tool 协议注册独立工具 `tokifactor_send_image`。Agent 可以调用它：

```json
{"path": "/tmp/example.png", "caption": "可选说明"}
```

- 只接受 PNG、JPEG、GIF、WebP，最大 8 MiB。
- 图片路径必须在 `/tmp`、`~/.local/share/tmp` 或用户已注册的可写目录内。
- 使用 tokifactor `PictureChunk` 协议加密分片发布到 `tokifactor/picture`。
- 可选 caption 会作为加密文本发布到 `tokifactor/text`。
- 工具默认自动通过；如需审批，可设置
  `AKI_TOKIFACTOR_IMAGE_REQUIRE_APPROVAL=1` 或启动时加
  `--tokifactor-image-require-approval`。启用后本地客户端或频道
  `akid-approve` 都可以批准。
- 返回 `transfer_id`、字节数、分片数和 MIME 类型。

### 摄像头

akid 通过 Letta external tool 注册 `camera_take_photo`。当前只支持单张拍照：

```json
{"device": "/dev/video0", "width": 1280, "height": 720}
```

- 默认使用 `ffmpeg` 从 V4L2 设备抓取一帧 JPEG，默认 `/dev/video0`、1280×720。
- 只接受 `/dev/videoN` 字符设备；图片输出到 `/tmp/aki/camera/`。
- 工具结果只包含路径、字节数、实际宽高、设备路径和 MIME，不包含图片内容；
  Agent 需要查看时自行 `Read`/`ViewImage`，也可以继续用
  `tokifactor_send_image` 发送该路径。
- **始终需要用户审批**。审批事件里的 `tool_name` 是 `camera_take_photo`，
  `channel` 是 `camera`；拒绝或超时不会打开摄像头。
- 文件位于 `/tmp`，重启后可能被系统清理。

相关环境变量：`AKI_CAMERA_ENABLED`、`AKI_CAMERA_REQUIRE_APPROVAL`、
`AKI_CAMERA_DEVICE`、`AKI_CAMERA_WIDTH`、`AKI_CAMERA_HEIGHT`、
`AKI_CAMERA_OUTPUT_DIR`、`AKI_FFMPEG_BIN`、`AKI_FFPROBE_BIN`。
`AKI_CAMERA_REQUIRE_APPROVAL=0` 只建议用于调试，默认必须保持 `1`。

### 桌面截图

akid 通过 Letta external tool 注册 `desktop_take_screenshot`：

```json
{"output": "DP-1", "include_cursor": false}
```

- KDE/Plasma 下只使用 `spectacle -b -n -f`；其他桌面使用 `grim`。
- KDE 下 Spectacle 失败或产出全黑图时直接报错，避免悄悄截到 cage 等
  非 KDE 合成器。
- 默认抓取整个桌面，输出 PNG 到 `/tmp/aki/screenshots/`；图片数据不会附在工具结果里，
  模型需要查看时自行 `Read`/`ViewImage` 该路径。
- 自动尝试当前 `WAYLAND_DISPLAY`，失败后回退到 runtime 目录下其他
  `wayland-*` socket。
- 只返回绝对路径、字节数、实际宽高、`Wayland display`、实际 backend、MIME 和
  cursor 状态；不返回图片内容。
- 默认**不需要审批**。如需恢复审批，可设置
  `AKI_SCREENSHOT_REQUIRE_APPROVAL=1`。

相关环境变量：`AKI_SCREENSHOT_ENABLED`、`AKI_SCREENSHOT_REQUIRE_APPROVAL`、
`AKI_GRIM_BIN`、`AKI_SPECTACLE_BIN`、`AKI_SCREENSHOT_OUTPUT_DIR`、
`AKI_SCREENSHOT_WAYLAND_DISPLAY`。

### 桌面实况上下文

akid 在每个 turn 真正开始前采集一次桌面快照，并把它原地写入 agent 系统提示词末尾的
唯一 `<aki-live-context>` 块。该块不会追加到用户消息或 `messages.jsonl`，因此模型每次
请求只看到一份当前快照，历史中没有重复副本。

块内包含：

- 当前本地时间、时区和 epoch。
- KDE 焦点窗口标题、应用名和 PID（通过 KWin scripting 读取 `workspace.activeWindow`，
  非交互式、不抓取鼠标；非 KDE 或失败时省略）。
- 按 `ps` 报告的 `%CPU` 排序的前 5 个进程。
- `$HISTFILE` 或 `~/.zsh_history` 中最近 20 条命令及时间戳；支持 zsh 扩展历史和多行命令。
- 截图成功时的路径、尺寸、后端和采集时间；图片字节不会附在提示词里，模型需要看画面时
  自行 `Read`/`ViewImage` 该路径。

截图走静默采集路径，不触发审批。任一来源失败只省略对应部分，不影响 turn。总块大小有
64K token 的保守上限（按 UTF-8 字节估算），默认启用；MQTT 来源的 turn 同样会获得该
上下文，因此不要在公共频道里逐字复述桌面信息。

相关环境变量和参数：`AKI_LIVE_CONTEXT_ENABLED`、`AKI_LIVE_CONTEXT_BUDGET_TOKENS`、
`AKI_ZSH_HISTORY`、`AKI_PS_BIN`、`AKI_QDBUS_BIN`、`AKI_LIVE_CONTEXT_TIMEOUT`；
`--no-live-context`、`--live-context-budget-tokens`、`--zsh-history`、`--ps-bin`、
`--qdbus-bin`、`--live-context-timeout`。`aki.health` 和 `aki.status` 的
`live_context` 字段只暴露采集状态和错误，不暴露块内容。

---

## 头像

网关启动时读取 `aki_avi.jpg`（默认项目根目录，可用 `AKI_AVATAR_PATH`
或 `--avatar-path` 覆盖），校验为 PNG/JPEG/GIF/WebP 且不超过 4 MiB。
该头像属于接口无关的 `AvatarAsset`，tokifactor 之外的未来接口可以复用同一
路径、字节、MIME 和 SHA-256，而不需要各自维护头像副本。

tokifactor 侧的行为：

- 连接后向 `tokifactor/avatar` 发布加密的 `have` 消息，包含头像哈希和头像数据。
- 在文本和图片消息的 `avatarHash` 字段中带上同一个哈希，让对端知道该向
  `akid` 请求头像。
- 收到针对 `akid` 的 `want` 时自动重发 `have`。
- 收到其他发送者的 `have` 时解密并校验 SHA-256，缓存到内存。

状态可以通过 `akictl avatar` 或 RPC `aki.avatar.status` 查询；`aki.health`
和 `aki.status` 也会返回 `avatar` 字段。

---

## 文件布局

- `engine/akid.py`：asyncio Unix socket server、JSON-RPC 分发、turn 队列与审批。
- `engine/letta_process.py`：Letta 子进程监督、崩溃重启、control request。
- `engine/writable.py`：可写目录注册表、路径校验与 bwrap 策略。
- `engine/avatar.py`：接口无关的头像资源（路径、MIME、SHA-256）。
- `engine/sandbox_exec.py`：在 bwrap 内启动 Letta 及其全部子进程。
- `engine/protocol.py`：NDJSON、JSON-RPC 错误码与事件模型。
- `engine/client.py`：同步客户端库与流式 REPL。
- `engine/akictl.py`、`bin/akictl`：对话 + 本地管理 CLI。
- `engine/gui_bridge.py`：GUI 专用本地 WebSocket → akid Unix socket 桥接。
- `install-widget.py`：安装 Plasma widget 和 `akid-gui.service`。
- `kde-widget/org.acite.aki.chat/`：Plasma 6 QML 聊天组件。
- `install-daemon.py`：systemd user service 安装器。
- `scripts/sync-persona.py`：把 `userdata` 模板渲染并写入 MemFS。
- `userdata/`：非隐私人格模板、`username.txt` 和同步清单。
- `tests/`：协议、网关、假 Letta 子进程与 CLI 端到端测试。

---

## 协议要点

- JSON-RPC 2.0，每行一个 JSON 对象，单行上限 1 MiB。
- 客户端先调用 `aki.hello`；`aki.health` 可在握手前调用。
- 方法：`aki.chat.send/cancel/history`、`aki.approval.respond`、
  `aki.memory.status/tokens/tree/read/search`、`aki.fs.writable.*`、
  `aki.mqtt.*`、`aki.username.get/set`、`aki.conversation.reset`、`aki.status`。
- 事件为 `aki.event` 通知，携带 `turn_id`、单调递增 `seq`、`type`、`data`、
  `timestamp`，只发送给该 turn 的发起连接。
- 同一时间只有一个活跃 turn；最多 32 个排队 turn，FIFO；同一连接内
  `client_message_id` 10 分钟内幂等。

---

## 测试

```bash
python3 -m unittest discover -v
```

测试覆盖 NDJSON 分帧、JSON-RPC 错误、FIFO/队列上限/幂等、事件序号、
多客户端隔离、断线取消、审批 allow/deny/timeout/断线、路径与 symlink 越界、
子进程崩溃重启、compaction 归一化、tokifactor 加密向量与分片重组、本地假
MQTT broker 收发、频道审批命令、bwrap 允许/拒绝写入、注册表持久化与吊销、
沙盒不可用 fail-closed、PictureChunk 编码、external tool 注册与审批、
图片源路径限制、MQTT 服务器持久化与热重连、`akictl` 管理命令、会话初始化、
人格模板渲染以及端到端流式输出。
