<h1 align="center">Aki</h1>

<p align="center">
  <strong>Local cyber lifeform · Unix socket gateway</strong><br/>
  <strong>Aki / 秋 / 小秋 / 秋（aki）</strong>
</p>

<p align="center">
  A local-first runtime for a persistent Letta agent: Unix-socket JSON-RPC, approvals, MemFS, MQTT, and tokifactor.
</p>

<p align="center">
  Python standard library · JSON-RPC over NDJSON · Letta stream-json · tokifactor · Legacy KDE Plasma 6 widget
</p>

---

## 仓库描述

> A local-first gateway for a persistent Aki agent: Unix socket, JSON-RPC, Letta, MQTT, and tokifactor.

Aki 是一个本地优先的持久化 Agent 运行时。`akid` 只监听 Unix socket，监督一个长驻的
`letta --input-format stream-json --output-format stream-json` 子进程，把对话、工具审批、
记忆、MQTT 频道和 tokifactor GUI 连接成一套可以长期运行的本地 Agent 系统。

这个仓库关心的不是“再做一个聊天 CLI”，而是几个更具体的问题：同一个 Aki 如何在多次
会话之间保持连续身份；危险操作如何由发起者批准；多个本地客户端如何共享状态而不共享
实时事件；以及人格和记忆如何被版本化、备份和恢复。

## 设计取向

- **本地优先。** 聊天入口只有一个 Unix socket；目录 `0700`、socket `0600`，
  监听后校验 `SO_PEERCRED` 与当前用户 UID。模型调用另走 `127.0.0.1:7721`，只给本机 Letta 用。
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
- **tokifactor GUI（首选）。** 在任意共享 `(broker host, port, magic)` 信道上探测 Aki；
  只有探测成功时才显示 Aki 专用状态栏（模型、reasoning、上下文占用、队列与审批）。
- **KDE Plasma 6 widget（legacy）。** 保留历史分页、流式输出、Markdown、工具调用卡片、
  GUI 审批卡和系统通知，但不再推荐安装。
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

akid 的设置在 `~/.aki/aki.conf`。第一次启动会按模板创建这份文件；如果 `~/.local/share/aki/` 里还有旧的 `mqtt.json`、`writable-dirs.json`、`mcp-native.json`，会把它们迁进去并移到 `~/.aki/migrated/`。上游密钥写在 `[upstream.*] api_key`。`api_key` 为空的模型不会暴露给 Letta。

Letta 只连接 `http://127.0.0.1:7721/v1`。akid 按请求里的模型把调用转到 conf 里的上游，并换上对应的密钥。`[proxy] upstream_proxy` 非空时，访问上游先走这台 HTTP 代理（HTTPS 用 CONNECT）；单个上游可在自己的 `[upstream.*]` 段设置 `use_proxy = false` 直连，本地 llama.cpp/OpenAI 兼容服务应这样配置。留空则所有上游直连。聊天客户端仍然只走 Unix socket。

默认 socket 为 `$XDG_RUNTIME_DIR/aki/akid.sock`。手动运行时：

```bash
AKI_SOCKET=/tmp/aki/akid.sock python3 engine/akid.py
```

### 首选 GUI：tokifactor

tokifactor 是当前首选的桌面/Android GUI。它不内置 Aki 专用 broker 预设，也不需要
固定 IP；在 tokifactor 设置里选择与 Aki 相同的 broker `host:port` 和 magic string 即可。
Aki 的 MQTT 配置在 `~/.aki/aki.conf` 的 `[mqtt]` 段：

```ini
[mqtt]
enabled = true
host = <broker-host>
port = 1883
topic = tokifactor/text
magic = <shared-magic>
```

把同样的 `host:port` 和 magic 填进 tokifactor。连接建立后，tokifactor 会在加密的
`tokifactor/control` 主题上发送 probe；如果信道里有 Aki，Aki 回复 presence 并每 10 秒
发布一次 status，tokifactor 顶部因此出现两行状态栏。信道里没有 Aki 时，tokifactor 只显示
普通聊天界面，旧 tokifactor 客户端也会忽略控制主题继续聊天。

### Legacy: KDE Plasma Widget (deprecated)

KDE Plasma widget 已弃用，保留代码仅为兼容和参考；新用户请使用 tokifactor。
安装器只有在显式传入 `--allow-deprecated` 时才会安装。

```bash
python3 install-widget.py --allow-deprecated --dry-run   # 预览 legacy unit 和安装路径
python3 install-widget.py --allow-deprecated             # 安装 legacy widget + GUI 桥接
python3 install-widget.py --status                       # --status 不受弃用限制
python3 install-widget.py --uninstall                    # 保留 akid、MemFS 和聊天历史
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
akictl mqtt set broker.example:1883  # 持久化并立即重连
akictl username                   # 查看当前称呼
akictl set-username Master        # 修改称呼并重编译当前会话
akictl recompile                  # 从 MemFS 重编译当前会话 prompt
akictl reset                      # 重置 default 会话（先备份）
```

可写目录、MQTT、MCP 排除列表和上游模型都写在 `~/.aki/aki.conf`（目录 `0700`、文件 `0600`）。
注册/吊销会立即重启 Letta 后端；若有活跃 turn，该 turn 会收到
`SANDBOX_POLICY_CHANGED` 和 `turn_end.reason=sandbox_restart`，队列中的其他 turn 保留。

---

## tokifactor MQTT 频道

akid 连接 `~/.aki/aki.conf` 中 `[mqtt]` 配置的信道，不假设固定 broker IP：

- Broker：`[mqtt] host:port`（用户实际配置的地址；私有 broker 仅作为当前部署示例）
- Topic：`tokifactor/text`
- Control topic：`tokifactor/control`
- Magic string：`[mqtt] magic`

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

### 控制主题与 Aki 探测

`tokifactor/control` 上的每条消息仍使用当前 magic 加密，不进入聊天历史、不创建
turn、不触发通知。协议版本为 `protocolVersion=1`：

- **Probe**：tokifactor 发布 `schema=akid.v1`、`kind=probe`、随机非空 `probeId` 和非空 text。
- **Presence**：Aki 回复 `kind=presence`、`agent=aki`、`agentName=Aki`、原样回传
  `probeId`，并附带 model、reasoning、capabilities、backendReady 和 channelReady。
- **Status**：Aki 连接成功后立即发布，此后每 10 秒心跳；turn 开始/结束、usage、
  审批请求/完成时也会立即刷新。字段包括模型、reasoning、上下文上限/用量、backend/
  channel/sandbox 就绪状态、队列深度、活跃 turn、待审批数、uptime 和 memory tokens。
- **Offline**：优雅停止时尽量发布 `kind=offline`；崩溃则由 tokifactor 的超时判定。

Aki 忽略错误 magic、自身消息和重复 `probeId`，并按 sender 做简单限流。tokifactor
连接成功、broker 或 magic 变化后发送 probe；未发现 Aki 时每 15 秒重试。收到 presence
或 status 后停止主动探测，改由心跳维持在线；连续 30 秒没有两者时隐藏 Aki UI 并恢复
probe。多个 Aki presence 时以首个有效 Aki 为准。

> 安全提示：公共 broker 不可信，magic string 是群组口令。任何知道该 magic
> string 的人都能读取频道、触发 Agent turn，并发送审批命令。不要在频道中
> 传播秘密；如需真实隔离，应换用私有 broker 和不同的 magic string。

---

## 工具

### MCP 原生工具

akid 会在 backend ready 时读取 Letta 的 MCP 配置，把每个 MCP 工具注册成模型原生的
external tool，名字保持 `mcp__<server>__<tool>`（例如
`mcp__tavily__tavily_search`）。模型直接调用这些工具，不再需要通过 Bash 执行
`letta mcp call`；akid 在收到 external tool 请求后代理到对应的 MCP server。

- 工具发现：`letta mcp list` + `letta mcp tools <server> --full`，注册前会等待发现完成，
  因此 backend 刚启动时第一个 turn 可能多等 1–3 秒。
- 审批策略与普通工具一致：`mcp__tavily__*` 自动放行；其他 MCP 工具默认需要审批。
- 默认排除 `local` 侦察工具，只注入 Tavily 的 5 个工具；避免 30 个侦察工具的
  schema 每轮占用上下文。需要时可用 `akictl` 打开：

  ```bash
  akictl mcp                 # 查看当前原生 MCP 工具状态
  akictl mcp local on        # 把 local 侦察工具也注入为原生工具
  akictl mcp local off       # 恢复默认，只保留 Tavily
  ```

  开关会写进 `~/.aki/aki.conf` 的 `[mcp] excluded`，并在切换时安全重启 backend；
  如果当时有 turn 在跑，该 turn 会被中断。
- 结果以文本返回模型，单次结果最多保留 128 KiB；图片内容目前以占位说明代替。
- MCP server 由 akid 以 external tool 方式启动，运行在 Letta bwrap 沙盒之外；
  只应配置可信的 MCP server。

这些值在 `~/.aki/aki.conf` 的 `[mcp]` 段：`native_tools`、`excluded`、`max_tools`、`timeout`。
`AKI_MCP_NATIVE_SERVERS` 仍是可选的逗号分隔白名单，默认空表示不按白名单裁剪。

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

摄像头开关、设备和输出目录在 `~/.aki/aki.conf` 的 `[camera]`。`require_approval` 默认 `true`。

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
- 默认**不需要审批**。如需恢复审批，把 `~/.aki/aki.conf` 里 `[screenshot] require_approval` 设为 `true`。

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
- `engine/gui_bridge.py`：legacy GUI 专用本地 WebSocket → akid Unix socket 桥接。
- `install-widget.py`：安装 legacy Plasma widget 和 `akid-gui.service`（需 `--allow-deprecated`）。
- `kde-widget/org.acite.aki.chat/`：legacy Plasma 6 QML 聊天组件。
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
控制主题 probe/presence/status、probe 去重与限流、人格模板渲染以及端到端流式输出。
