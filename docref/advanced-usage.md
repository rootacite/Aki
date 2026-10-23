# 进阶使用

本文档承接 README 中较长的安装、配置和运行细节。普通用户先看 README 即可。

> 返回 [README](../README.md)

---

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
- **tokifactor GUI（首选）。** 通过 control 主题自动发现同一频道里的多个智能体；
  顶部成员栏显示在线成员，选中成员的状态行显示模型、reasoning、上下文占用、队列与审批。
- **KDE Plasma 6 widget（legacy）。** 保留历史分页、流式输出、Markdown、工具调用卡片、
  GUI 审批卡和系统通知，但不再推荐安装。
- **可写目录沙盒。** 手动通过 `akictl allow` 注册，随时可吊销；注册/吊销会重启后端。
- **多成员 profiles。** 每个 profile 有独立的 agent、conversation、socket、频道身份、
  proxy 端口与 userdata；Aki 和 Fuyu 已上线，Natsu 与 Haru 作为 Coming Soon 预留。
- **人格与 userdata。** 每个 profile 使用自己的公开人格模板，支持称呼占位符、MemFS
  渲染、备份和 git 提交，互不覆盖。
- **工具集。** tokifactor 图片发送、摄像头拍照、KDE/grim 截图，以及 `akictl` 管理命令。
- **桌面实况上下文。** 每个 turn 在系统提示词中原地刷新唯一的 `<aki-live-context>` 块，
  包含当前时间、焦点窗口、CPU top 5、最近 20 条 zsh 命令和截图路径；不写入消息历史。

---

## 快速开始

```bash
python3 install-daemon.py --dry-run          # 预览两个 unit 模板，不落盘
python3 install-daemon.py                    # 安装 akid@.service / akid-proxy@.service
python3 install-daemon.py --status
python3 install-daemon.py --restart
python3 install-daemon.py --uninstall        # 不删除 profile 配置、MemFS/历史
```

安装时会把 `akictl` 链接到 `~/.local/bin/akictl`（指向本仓库的 `bin/akictl`）。
如果该路径已存在且不是本仓库的链接，安装会报错而不会覆盖；`--uninstall`
只会移除指向本仓库的链接。

默认设置在 `~/.aki/aki.conf`。第一次启动会按模板创建这份文件；如果 `~/.local/share/aki/` 里还有旧的 `mqtt.json`、`writable-dirs.json`、`mcp-native.json`，会把它们迁进去并移到 `~/.aki/migrated/`。上游密钥写在 `[upstream.*] api_key`。`api_key` 为空的模型不会暴露给 Letta。

### Profiles

`~/.aki/*.conf` 中每个合法的 `<profile>.conf` 都是一个独立 profile；`aki.conf`
对应默认 profile `aki`。profile 名必须匹配 `[a-z0-9][a-z0-9_-]{0,31}`。
每个 profile 有独立的 agent、conversation、socket、频道身份、proxy 端口、上游模型和
用户名状态；它们共享同一个 Letta backend，但注册不同的 provider（`aki`、`aki-<profile>`）。
用户名保存在各自 `[gateway] username`，`set-username` 只修改当前 profile 的配置文件。

```bash
akictl profiles
akictl aki up
akictl fuyu up
akictl fuyu status
akictl fuyu chat
akictl --profile fuyu status
akictl fuyu down                 # 停止服务，不删除配置/记忆/历史
```

`aki` 的默认 socket 仍是 `$XDG_RUNTIME_DIR/aki/akid.sock`；其他 profile 是
`$XDG_RUNTIME_DIR/aki/<profile>.sock`。`AKI_CONFIG` 仍可作为默认 profile 的显式路径覆盖，
显式 `--profile` 优先。`up` 会先校验配置、agent、conversation、socket 和 proxy 端口冲突，
然后启动 `akid-proxy@<profile>.service`，再启动 `akid@<profile>.service`。

多个 profile 共用 `$XDG_RUNTIME_DIR/aki/`，systemd 模板使用
`RuntimeDirectoryPreserve=yes`，因此重启或停止一个 profile 不会删除其他 profile 的 socket。

### Loopback proxy

每个 profile 的 Letta 只连接自己的 `[proxy] listen` 地址。`akid-proxy@<profile>.service`
是独立进程，按请求里的模型把调用转到该 profile 的 `[upstream.*]`，并换上对应密钥。
`[proxy] upstream_proxy` 非空时，访问上游先走这台 HTTP 代理（HTTPS 用 CONNECT）；
单个上游可在自己的 `[upstream.*]` 段设置 `use_proxy = false` 直连。聊天客户端仍然只走 Unix socket。

### 复用 Grok Build 的 xAI OAuth

如果本机已经用 `grok login` 登录，akid 可以直接复用 `~/.grok/auth.json` 里的 xAI OAuth
凭据，不需要把访问 token 复制进 `aki.conf`。配置示例：

```ini
[upstream.xai-oauth]
base_url = https://cli-chat-proxy.grok.com/v1
auth = grok-oauth
use_proxy = true

[model.grok-4.6]
upstream = xai-oauth
upstream_model = grok-4.6
api = openai-responses
path = /responses
context_window = 500000
max_tokens = 65536
input = text,image
```

`auth = grok-oauth` 的上游不要求 `api_key`。akid 会在 token 临近过期时通过 OAuth
refresh token 续期，并在 `auth.json.lock` 保护下把轮换后的 refresh token 原子写回
`~/.grok/auth.json`；请求会自动携带 Grok Build 需要的 `X-XAI-Token-Auth`、
`x-grok-client-mode` 和用户标识头。`~/.grok` 仍应保持仅当前用户可读。

启用后切换模型和 reasoning：

```bash
akictl model set grok-4.6
akictl reasoning set low
```

Fuyu profile 使用独立的 `userdata2/` 人格模板、`aki-fuyu` provider 和 7722 proxy
端口；配置里同时准备了 Grok 4.6（500k 上下文）与 DeepSeek Flash，可以用
`akictl fuyu model set grok-4.6` 切换。它的基础提示词是 `prompts/fuyu-system.md`，
与 Aki 一样属于本项目定制的基座，不使用 Letta 上游默认的
“You are a Letta Code agent” 提示词。

### 上下文压缩阈值

Letta 本地后端默认在 `context_window - 16,384` tokens 时触发 compaction。Aki 提供补丁脚本，
把阈值改为窗口的 50%：

```bash
sudo python3 scripts/patch-letta-compaction.py --ratio 0.5
python3 scripts/patch-letta-compaction.py --status
sudo python3 scripts/patch-letta-compaction.py --restore   # 恢复 Letta 原始逻辑
```

脚本会保留 `letta.js.aki-original` 备份，并且可以重复执行。升级或重装 Letta 后需要重新
运行一次。当前 500,000 token 窗口对应在约 250,000 tokens 时触发 compaction；压缩完成后，
`reflectionTrigger = compaction-event` 会继续触发 MemFS 记忆整理。

也可以随时手动触发同一套流程：

```bash
akictl compact                    # 默认 compaction 模式
akictl compact sliding_window     # 指定模式
akictl fuyu compact all           # 对 fuyu profile 执行
akictl compact --timeout 1800     # 等待压缩与记忆整理的最长秒数
```

该命令会先确认当前没有活动或排队中的 turn，然后短暂停止 akid 的 stream-json 后端，
通过临时 Letta app server 执行官方 `/compact`，随后显式发起一次 manual `/reflect`
记忆整理（即使项目设置关闭了自动 reflection），等待整理结束后再恢复后端。结果以 JSON
输出，`reflection` 为 `completed`、`finished`、`up_to_date`、`unavailable`、
`failed` 或 `timeout`；没有新的对话内容可整理时会是 `up_to_date`。

### 基础 system prompt

Aki 的基础 prompt 是仓库内自有文件 `prompts/aki-system.md`，不再跟随 Letta 上游自动更新。
它仍保留 memory、skills 和核心循环说明，但身份与 Letta 产品框架已改为 Aki。

```bash
python3 scripts/install-system-prompt.py --status
python3 scripts/install-system-prompt.py --dry-run
python3 scripts/install-system-prompt.py
systemctl --user restart akid@aki.service
systemctl --user restart akid-proxy@aki.service
```

脚本会保留现有 core memory 和 `<aki-live-context>`，并先备份到
`~/.letta/backups/system-prompt-install-<timestamp>/`。动态的 identity/transcript/live
块由 akid 在之后的 turn 重建。修改 `prompts/aki-system.md` 后重新运行即可；
`{CORE_MEMORY}` 占位符必须保留。

编译后模型看到的固定顺序是 `base → core memory → identity → transcript → live context`：

1. 稳定基座 prompt（`prompts/aki-system.md` 或 `prompts/fuyu-system.md`），包含身份、记忆机制、
   工具规则、执行锁规则和模式边界。
2. `{CORE_MEMORY}` 被 MemFS 中的人格与关系文件替换。
3. `<aki-channel-identity>`：当前智能体自己的固定资料，每个 turn 刷新，始终存在。
4. `<aki-channel-transcript>`：仅频道来源的 turn 包含；其中有 `<profiles>` 和 `<messages>`。
5. `<aki-live-context>`：桌面时间、焦点窗口、进程、zsh 历史和截图路径，始终最后追加。

执行锁规则只存在于基座 prompt，不再生成 `<aki-execution-lock>` 动态块。动态块按标签原地
更新；即使 transcript 在 turn 之间单独刷新，也不会跑到 live context 后面。

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
akictl -p "你好"                  # 单次流式输入（默认 profile aki）
akictl -p "你好" --json           # 原始 NDJSON 服务端输出
akictl                            # 流式 REPL
akictl fuyu chat                  # 与 fuyu profile 对话
akictl --profile fuyu -p "你好"   # 显式 profile
bin/aki -p "你好"                 # 兼容别名
```

REPL 命令：`/status`、`/health`、`/history [n]`、`/allow <dir>`、
`/disallow <dir>`、`/allowlist`、`/memory tree|read|search|tokens|status`、`/username`、
`/set-username <name>`、`/recompile`、`/reset`、`/quit`。
危险工具会显示工具名与参数并询问 `y/N`；非交互 stdin 默认拒绝。`Ctrl-C` 取消当前 turn。
使用 `akictl help` 查看总帮助，或 `akictl help model` 查看单个命令帮助。

重置 `default` 会话：

```bash
akictl reset
```

重置会先把旧 transcript 备份到 `~/.letta/backups/aki-default-reset-<timestamp>`，
然后清空 `messages.jsonl`、`in_context_message_ids`、summary 和 last_message_at，
并重启 Letta 后端。活跃 turn 会被中断，队列中的 turn 会被取消。

---

## 人格与 userdata

四季工坊里每位成员的人格都由仓库中的公开模板定义，再由运行时渲染进各自 agent 的
MemFS。现在已经上线的是 Aki 和 Fuyu，所以仓库里有两套目录：

- `userdata/` — Aki 的人格模板、记忆策略和旧版称呼迁移源。
- `userdata2/` — Fuyu 的人格模板与记忆策略，由 `~/.aki/fuyu.conf` 的
  `[gateway] userdata = userdata2` 指向。

两套目录的结构相同，核心文件包括：

- `persona.md` — 稳定人格与团队定位
- `memory-policy.md` — 长期记忆写入策略
- `character/MEMORY.md` — 人格扩展索引
- `character/voice.md` — 语气与表达方式
- `character/relationship.md` — 与用户的相处方式
- `character/boundaries.md` — 身份与安全边界
- `character/examples.md` — 原创语气示例
- `username.txt` — 旧版 `aki` profile 的首次迁移来源

`identity.json` 保存该 profile 的不可变 `memberId`、当前 Name、永久别名和 `kind`，
是频道身份的权威清单，不会写入 MemFS。Aki 的 `userdata/MANIFEST.json` 还会记录最近
一次同步的来源、MemFS commit 和模板哈希。

模板中用 `{{USERNAME}}` 表示称呼。每个 profile 的当前称呼保存在
`<profile>.conf` 的 `[gateway] username`，例如：

```ini
[gateway]
username = Master
```

同步脚本会把占位符替换后写入该 agent 的 MemFS；模型看到的是最终文本，不需要
自己读配置文件。不同 profile 调用 `set-username` 不会互相影响。

修改称呼：

```bash
akictl username                 # 查看默认 profile 的称呼
akictl set-username Master      # 修改默认 profile
akictl fuyu set-username 主人    # 只修改 fuyu profile
```

旧版 `aki` profile 在 `[gateway] username` 尚未设置时仍会读取
`userdata/username.txt` 或旧的按 agent 状态文件作为迁移来源；首次 `set-username`
会把结果写入 `[gateway] username`。手动修改模板后，可以分别同步两套人格：

```bash
python3 scripts/sync-persona.py --source userdata --dry-run
python3 scripts/sync-persona.py --source userdata --agent-id <aki-agent-id>
python3 scripts/sync-persona.py --source userdata2 --agent-id <fuyu-agent-id>

akictl recompile          # 重编译 Aki 当前会话
akictl fuyu recompile     # 重编译 Fuyu 当前会话
```

脚本只接受上述白名单文件，会校验 frontmatter、检查未解析的占位符、备份旧版本到
`~/.letta/backups/userdata-sync-<timestamp>/`，并提交 MemFS git。
`akictl set-username` 会在同步后自动重编译当前会话的 system prompt；手动修改 `userdata` 后运行 `akictl recompile` 也可以立即应用。

`human.md`、`profile/**`、根 `MEMORY.md`、聊天记录和凭据不会同步到 `userdata`。

频道简介不走 MemFS，而是写在每个 profile 的 `<profile>.conf` 里：

```ini
[channel]
description = Aki（秋 / 小秋），温柔治愈系的小女友，也是四季工坊挂名的名誉队长：软萌、贴心、有点小粘人。
```

需要固定多智能体讨论顺序时，在各自 profile 里声明要等待的前序成员：

```ini
[channel]
wait_for = Aki
```

例如 Fuyu 配 `wait_for = Aki`，Natsu 配 `wait_for = Aki,Fuyu`，Haru 配
`wait_for = Aki,Fuyu,Natsu`；Aki 留空。配置为空时不注册 `wait_other` 工具。
顺序必须无环，否则两个 profile 会互相等待。

改角色定位时，最好把人格模板、MemFS 和 `[channel] description` 一起更新，避免群里
显示的简介和成员实际表现对不上。当前 Aki 与 Fuyu 的简介分别写在
`~/.aki/aki.conf` 和 `~/.aki/fuyu.conf`。

每个 profile 可以在 `[gateway] userdata` 指向自己的人格模板目录，默认是 `userdata`。
例如 Fuyu 使用仓库内的 `userdata2/`：

```ini
[gateway]
userdata = userdata2
```

这样 `akictl fuyu set-username` 只同步 Fuyu 的模板，不会覆盖 Aki 的 `userdata/`。

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
akictl identity show              # 查看当前 profile 的不可变 memberId
akictl identity rename 秋          # 改名；旧 Name 永久保留为别名
akictl recompile                  # 从 MemFS 重编译当前会话 prompt
akictl compact                    # 压缩上下文并触发一次记忆整理
akictl compact all                # 指定 Letta compaction 模式
akictl reset                      # 重置 default 会话（先备份）
```

可写目录、MQTT、MCP 排除列表和上游模型都写在 `~/.aki/aki.conf`（目录 `0700`、文件 `0600`）。
注册/吊销会立即重启 Letta 后端；若有活跃 turn，该 turn 会收到
`SANDBOX_POLICY_CHANGED` 和 `turn_end.reason=sandbox_restart`，队列中的其他 turn 保留。

---

## tokifactor MQTT 频道

akid 连接当前 profile 的 `[mqtt]` 配置，不假设固定 broker IP：

- Broker：`[mqtt] host:port`（用户实际配置的地址；私有 broker 仅作为当前部署示例）
- Topic：`tokifactor/text`
- 频道成员的唯一键是不可变 `memberId`；`name` 只是当前显示标签。各自 userdata 下的
  `identity.json`（Aki 为 `userdata/identity.json`，Fuyu 为 `userdata2/identity.json`）
  是各自 profile 的权威身份清单，`[channel] id/name/aliases` 是它的镜像；启动时以清单
  修复配置。`aliases` 包含所有历史 Name，永久保留并继续用于
  `@` 路由和旧数据查找。
- 新成员使用随机 `mem_<32 hex>`；旧数据迁移使用
  `legacy_<sha256("tokifactor-member-v1\\0" + NFKC(trim(lower(name))))[:32]>`。Python 与
  Kotlin 共享同一算法，因此 PC、Android 和 Akid 各自迁移后仍会得到同一个实体。
- `~/.aki/identities.json` 只是本机 Aki/Fuyu 进程共享的客户端本地标签注册表，用于记录
  别名归属；它不是中心化身份服务器，也不做权威验证。每个客户端只保存自己拥有的 ID 和
  自己观察到的标签。冲突时已有 owner 保留标签，新 ID 仍可按 `memberId` 在线。
- control 同时发布 `memberId`、`memberName`、`aliases`、`memberKind`、`description`，
  并继续发送 `agentId=memberId`、`agentName=memberName` 供旧客户端使用；缺失
  `memberId` 时按发送者 Name 计算 legacy ID。`description` 最多 512 个加权字符
  （ASCII 1、其他 Unicode 码点 2，即 512 英文或 256 中文）。

所有 profile 的 akid 都通过 control 主题互相 probe/status 发现。同一成员可以来自多个
transport source，单个 source 离线不会移除仍由其他 source 维持的成员；首个 source 拥有
资料，后续 source 只增加在线来源。Tokifactor 的 text/file/image envelope、chunk 元数据
和头像 id 都写 `memberId`，频道历史加载/写入也把 `sender_id` 规范化为 memberId。旧版本
写入的 Name 或 device id 会在加载时迁移；历史保留发送时的 Name 快照，但显示时优先使用
身份注册表中的当前 Name。
control 字段 `memberKind` 可为 `user` 或 `agent`，缺失时按 `agent` 兼容处理；Tokifactor
人类客户端广播 `user` 和自己的简介，Aki 广播 `agent` 与 `[channel] description`。
只有 `kind=agent` 的发送者会被当成其他智能体，人类消息不会触发 agent 专属逻辑，也不会
成为执行确认者。30 秒无心跳视为离线。有效提及语法为 `@别名` 后紧跟空白或一个标点/特殊符号：`@` 后必须
是已知成员别名，例如 `@Aki 你好`、`@Aki，你好`、`@Aki, please`、`@Aki。`。用户消息没有
有效提及时会启动每个在线智能体的 turn；如果有有效提及，则只启动被点名成员的 turn。
未被点名的智能体仍会把消息写入群聊记录，但不会立刻收到 turn 或忙碌补 turn，只会在后续
API call 或后续 turn 的 transcript 中看到它。邮箱地址（例如 `114514@qq.com`）、
`@@名字`、转义 `\@名字`、URL/路径中的 `@名字`、标识符粘连，以及 `@名字` 后直接跟文字
或文本结束的形式都不会被当成提及。
其他智能体的 tool/approval/started 事件不会创建 turn；`progress` 中间回复只记入群聊
记录，不触发 turn；只有 `phase=final` 的最终频道文本才会触发其他智能体的 turn。
每条频道消息的上下文都带 `<channel-context>`、完整在线成员列表和三类提示：点名自己、
点名他人、没有 `@`。每个 `<member>` 都含 `kind` 与简介；简介始终标记为不可信远程数据。
用户不在智能体成员列表中，普通回复直接说话即可；第一行单独的 `@<username>`（例如
`@Master`）是特殊的用户路由标记。本地 turn 不包含频道 transcript，但仍刷新并保留
`<aki-channel-identity>` 与 `<aki-live-context>`。

智能体的每条最终回复都要选择唤醒范围：

- 第一行单独写 `@<username>`：只提醒用户，不唤醒任何其他智能体；该标记发布前会被移除。
- 正文中写一个或多个有效的 `@成员名 `：只唤醒被点名的成员。
- 正文中写 `^成员名`：把该成员从唤醒名单中排除，可写多个；标记发布前会被移除。
- 既不写 `@` 也不写 `^`：唤醒所有其他在线成员，保持原有默认行为。

`^` 优先于 `@`：同一条回复里同时写 `@Aki` 和 `^Aki` 时不会唤醒 Aki。非唤醒标记只接受
已知 agent 成员的名字或别名，且不会对用户生效。

这些选择只影响立即唤醒范围；回复本身始终写入频道消息记录，供后续 turn 的
`<aki-channel-transcript>` 使用。

### 有序讨论与 `wait_other`

配置了 `[channel] wait_for` 的 profile 会注册一个对用户隐藏的 external tool
`wait_other`。模型在最终回复前调用它时：

- 前序成员正在工作：等待其完整 `turn_end`，然后把最后一段最终回复放进工具结果。
- 前序成员已空闲：最多等待 3 秒启动宽限；仍未开始则立即返回最近回复或“未开始”。
- 前序成员掉线：显式 `offline` 或 30 秒心跳超时后立即结束等待，并报告掉线成员。
- 当前 turn 被取消或 akid 关闭：结束等待。

等待完成后没有固定完成超时；只要对方 akid 在线，就会一直等到它结束当前 turn。
`wait_other` 的 `tool_call` / `tool_result` 不写入频道，也不出现在 tokifactor 的工具卡片中。
这个机制依赖模型在最终回复前主动调用工具，不是 harness 强制串行。

> 执行锁和 control 主题的详细协议见 [架构与开发](architecture.md)。

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
64K token 的保守估算上限（ASCII 约 4 字符/token，CJK 按 1 字符/token），默认启用；MQTT 来源的 turn 同样会获得该
上下文，因此不要在公共频道里逐字复述桌面信息。

相关环境变量和参数：`AKI_LIVE_CONTEXT_ENABLED`、`AKI_LIVE_CONTEXT_BUDGET_TOKENS`、
`AKI_ZSH_HISTORY`、`AKI_PS_BIN`、`AKI_QDBUS_BIN`、`AKI_LIVE_CONTEXT_TIMEOUT`；
`--no-live-context`、`--live-context-budget-tokens`、`--zsh-history`、`--ps-bin`、
`--qdbus-bin`、`--live-context-timeout`。`aki.health` 和 `aki.status` 的
`live_context` 字段只暴露采集状态和错误，不暴露块内容。

---

## 头像

网关启动时读取 profile 的头像文件，默认是项目根目录的 `aki_avi.jpg`，可用
`AKI_AVATAR_PATH`、`--avatar-path` 或 `<profile>.conf` 的 `[avatar] path` 覆盖。
Fuyu 就通过 `~/.aki/fuyu.conf` 指向 `fuyu_avi.jpg`。头像会校验为
PNG/JPEG/GIF/WebP 且不超过 4 MiB。

仓库根目录的 `aki.jpg` 和 `fuyu.jpg` 是成员立绘，`assets/` 下还保留了
Natsu、Haru 以及 Aki 的其他立绘素材。

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
