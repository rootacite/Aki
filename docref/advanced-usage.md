# 进阶使用

本文档承接 README 中较长的安装、配置和运行细节。普通用户先看 README 即可。

> 返回 [README](../README.md)

---

## 功能

- **Unix socket JSON-RPC 网关。** NDJSON，每行一个 JSON 对象，单行上限 1 MiB；
  支持 `aki.hello`、`aki.approval.*`（仅 MQTT turn）、`aki.memory.*`、`aki.fs.writable.*`、
  `aki.mqtt.*`、`aki.username.*`、`aki.conversation.reset`。
- **长驻 Letta 监督。** 等待 `system/init` 后 ready；异常退出按 1/2/4/8/16/30 秒退避重启；
  当前 turn 标记失败，队列保留。
- **多客户端。** 状态、历史和只读记忆共享；实时事件通常只发给该 turn 的发起连接，
  但频道 turn 的审批请求/结果会额外广播给本机客户端，方便用户在任意 GUI 里处理。
- **审批工作流。** `Read`、`ReadLSP`、`Grep`、`Glob`、`LS`、`Memory`、`ViewImage` 和
  `mcp__tavily__*` 自动放行；`Bash` 在 sandbox 就绪时也默认放行。`Write`/`Edit`/
  `ApplyPatch` 和未知工具需要发起客户端批准，默认 60 秒超时拒绝。
- **tokifactor MQTT 频道。** 加密文本/图片、分片消息、头像发布、频道内审批，以及
  `akid.v1` 结构化事件。
- **tokifactor GUI（首选）。** 通过 control 主题自动发现同一频道里的多个智能体；
  顶部成员栏显示在线成员，选中成员的状态行显示模型、reasoning、上下文占用、队列与审批。
- **唯一对话入口。** 真人消息只通过 MQTT `kind=message` 进入；`aki.chat.*`、`bin/aki`、
  KDE widget 和 GUI bridge 已移除。
- **可写目录沙盒。** 手动通过 `akictl allow` 注册，随时可吊销；注册/吊销会重启后端。
- **多成员 profiles。** 每个 profile 有独立的 agent、conversation、socket、频道身份、
  proxy 端口与 userdata；Aki、Fuyu 和 Haru 已上线，Natsu 作为 Coming Soon 预留。
- **人格与 userdata。** 每个 profile 使用自己的公开人格模板，支持称呼占位符、MemFS
  渲染、备份和 git 提交，互不覆盖。
- **工具集。** tokifactor 图片发送、摄像头拍照、KDE/grim 截图，以及 `akictl` 管理命令。
- **桌面实况上下文。** 每个 turn 采集一次快照；bridge 模式下 `<live-context>` 作为 per-turn overlay 注入实际 API 请求，不落盘，
  包含当前时间、焦点窗口、CPU top 5、最近 20 条 zsh 命令和截图路径；不写入消息历史。
- **运行中消息 overlay。** turn 运行期间到达的真人补充消息会原子写入
  `AKI_TURN_OVERLAY_DIR/<turn_id>/`（默认在 backend 目录下的
  `aki-turn-overlays/<agent_id>`，目录 `0700`、文件 `0600`）。每条文件带稳定
  `insertion_id`、seq 和插入时间，正文块为 `<user-insertion>`。bridge 只删除文件一次，
  但会在该 turn 后续每次 provider 请求中重放 `<user-insertions count=N>`，直到 turn 结束；
  没有后续 API call 时，turn 退出会把未消费消息合并成一个新的持久用户 turn。已消费消息
  并入当前 turn 最近一条真人 user role 的单个 `<user-insertions>` 块；智能体触发的临时
  turn 先回滚临时 prompt/assistant/tool 消息再合并。群聊记录本身在消息送达时立即追加为
  普通消息，多条插入不会在群聊历史中合并。

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

### Vendored Letta runtime

Letta Code 以 submodule 形式固定在 `vendor/letta-code`（当前 `v0.34.24`）。
生产服务只使用本仓库构建的运行时，不依赖 `PATH` 里的系统 `letta`：

```bash
git submodule update --init --recursive
python3 scripts/build-letta.py --build --install
python3 scripts/build-letta.py --check
./bin/letta-aki --version
```

构建使用用户级 Bun `1.3.14`（缓存在 `~/.cache/aki/toolchain/`），补丁位于
`patches/letta-code/`，版本与补丁哈希记录在 `vendor/letta-code.lock.json`。
运行时安装到 `~/.local/lib/aki/letta-code/<version>/`，稳定入口是
`~/.local/lib/aki/letta-code/current`；`bin/letta-aki` 负责解析该入口并以
`/usr/bin/node` 启动。运行时缺失或版本不匹配时 akid fail-closed，不会回退到系统
`letta`。`install-daemon.py` 会自动检查并在缺失时构建运行时。

默认设置在 `~/.aki/aki.conf`。第一次启动会按模板创建这份文件；如果 `~/.local/share/aki/` 里还有旧的 `mqtt.json`、`writable-dirs.json`、`mcp-native.json`，会把它们迁进去并移到 `~/.aki/migrated/`。上游密钥写在 `[upstream.*] api_key`。`api_key` 为空的模型不会暴露给 Letta。

各成员的采样参数固定在该 profile 的 `[model.*] extra_body` 中，由本地代理合并进每次上游请求：
Aki `temperature=0.67` / `top_p=0.83`，Fuyu `0.7` / `0.86`，Haru `0.88` / `0.92`。
新增模型路由时也要带上对应成员的 `extra_body`；`extra_body` 不能覆盖 `model` 等路由字段。

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

Haru profile 使用 `userdata3/`、`aki-haru` provider 和 7723 proxy 端口，默认模型句柄
是 `aki-haru/gemini-3.8-flash`。她的 `[tools]` 段显式把工具收窄为 `Read,Write,Edit,ViewImage,
Memory,Skill,acquire_execution_lock,wait_other,wait_user,desktop_take_screenshot,camera_take_photo,tokifactor_send_image,generate_image`，并把 `Bash/exec_command/write_stdin` 与子代理工具
从 schema 中移除。写入只允许 `/tmp` 与 `~/.local/share/tmp`；`[writable] dirs` 保持为空，
bubblewrap 是第二层边界。`Read` 也被限制在 `/tmp`、`~/.local/share/tmp` 和 Letta 自己的
MemFS/工具归档目录，不能用来翻仓库代码、测试、配置或日志；代码问题会直接转交。第一版接入
本机 `cli-proxy-api` 提供的 Gemini 3.8 Flash 文本 API；key 未填写时模型不会暴露给 Letta。

Gemini 3.8 Flash 路由默认使用 1,000,000 上下文和 65,536 输出预算，并通过
`extra_body` 发送 `{"reasoning_effort": "low"}`。`low` 用于降低创意对话的延迟和过度
推理；如果后续需要更深的规划，可改为 `medium` 或 `high`。上游是 Aki 共用的本机
`cli-proxy-api`（`127.0.0.1:8317`），使用 `use_proxy = false` 直连。文生图走独立的
MiniMax `image-01` API；TTS、视频与 ComfyUI 仍属于后续阶段，Haru 的人格和提示词会明确
说明这些能力尚未安装。

在新机器上首次创建 Haru 的 profile、agent、conversation、MemFS 和 system prompt：

```bash
python3 scripts/bootstrap-member.py --profile haru
```

脚本默认只创建新的 agent/conversation；目标已存在时不做修改。它会调用现有的
`sync-persona.py` 和 `install-system-prompt.py`。完成后把本机 `cli-proxy-api` 的 key 填进
`~/.aki/haru.conf` 的 `[upstream.cli-proxy-api] api_key`（通常与 `aki.conf` 相同），把
MiniMax key 填进 `[upstream.minimax] api_key`，再启动
`akid-proxy@haru.service` 与 `akid@haru.service`。

### 上下文压缩阈值

vendored runtime 的 compaction 阈值由 `AKI_COMPACTION_RATIO` 控制，默认 `0.5`：

```bash
systemctl --user edit akid@.service   # Environment="AKI_COMPACTION_RATIO=0.5"
```

该行为由 `patches/letta-code/0002-aki-compaction-policy.patch` 实现；修改比例后需要重新构建
并重启对应服务（默认值已内置为 0.5，通常不需要显式设置）。当前 500,000 token 窗口对应在约 250,000 tokens 时触发 compaction。压缩完成后不会单独触发 reflection；当前项目使用 `reflectionTrigger = step-count`（阈值 25），累计 25 个未反思 step 后触发 MemFS 记忆整理，backend 通过 `--reflection-trigger step-count --reflection-step-count 25` 显式传入。旧的
`scripts/patch-letta-compaction.py` 仅用于尚未迁移的外部 Letta 安装。

也可以随时手动触发同一套流程：

```bash
akictl compact                    # 默认：保留最新 40% 消息，摘要其余部分
akictl compact sliding_window     # 指定模式
akictl compact sliding_window --omit-tool-results  # 成功后把持久 toolResult 正文替换为 [Omitted]
akictl fuyu compact all           # 对 fuyu profile 执行
akictl compact --timeout 1800     # 等待压缩与记忆整理的最长秒数
```

默认 `sliding_window` 按**消息条数**保留最新约 40%：

```text
retainCount = ceil(messages.length * 0.4)
cutoffIndex = messages.length - retainCount
```

切点如果落在 `toolResult` 上，会向前对齐，避免保留没有对应 tool call 的孤立工具结果。
切点之前的消息生成摘要，切点及之后的消息原样保留；压缩完成后继续触发 `aki_reflect`
把本轮压缩事件交给 MemFS 记忆整理。`all` 模式仍然摘要全部消息，不保留原始消息窗口。
该保留窗口逻辑由 `patches/letta-code/0004-aki-compact-retain-latest.patch` 实现。

该命令会先确认当前没有活动或排队中的 turn，然后通过长驻 Letta 进程的 `aki_compact`
控制请求执行 compaction，再通过 `aki_reflect` 发起 manual reflection；不再启动临时
app-server，也不停止 stream-json 后端。结果以 JSON 输出，`reflection` 为
`completed`、`finished`、`up_to_date`、`unavailable`、`failed` 或 `timeout`；
没有新的对话内容可整理时会是 `up_to_date`。

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

脚本只安装静态 base 到 Letta 的 `agent.system`，不再读写
`conversation/system-prompt.json`。core memory 由 Letta/MemFS 编译；所有 Aki 运行时块
（ownership、policy、wake routing、identity、solo memory、transcript、roster、
live context）都由 akid 在请求前作为 turn overlay 注入。脚本会先备份到
`~/.letta/backups/system-prompt-install-<timestamp>/`。修改 `prompts/aki-system.md` 后
重新运行即可；`{CORE_MEMORY}` 占位符必须保留。

编译后模型看到的固定顺序是 `base → core memory → stable system overlay → messages →
mid-system tail`。stable system overlay 内部顺序为 `tool ownership → channel policy →
wake routing → identity → roster → format guard`；mid-system tail 为
`turn control → transcript → solo mode → solo memories → solo transcript → live context`：

1. 稳定基座 prompt（`prompts/aki-system.md`、`prompts/fuyu-system.md` 或
   `prompts/haru-system.md`），包含身份、记忆机制、工具规则、执行锁规则和模式边界。
2. `{CORE_MEMORY}` 被 MemFS 中的人格与关系文件替换。
3. `<tool-ownership>`：当前 profile 可用的专属工具、禁止调用的他人工具、任务归属和
   `@Owner` 转述规则，每个 turn 作为 overlay 注入。
4. `<channel-policy>`：频道静态规则，包括 untrusted roster 说明、mention 语法、
   execution guidance、专属域规则和输出规则。
5. `<channel-wake-routing>`：英文 overlay 块，包含 `@成员`、`^成员` 的最终回复唤醒规则，以及“只回复用户时
   使用 `@<username>`”和 `^` 排除提示；每个 turn 按 profile username 重新渲染。
6. `<channel-identity>`：当前智能体自己的固定资料，每个 turn 作为 overlay 注入。
7. `<channel-context>`：当前在线成员 roster，作为稳定 system overlay 注入。
8. `<format-guard>`：Aki、Fuyu、Haru 全部启用，使用各自 username，作为稳定 system overlay 注入。
9. `<turn-control>`：当前频道 turn 的 guidance、initiation budget、execution responder、
   `wait_user` / `wait_other`；不复制消息正文。位于 mid-system tail。
10. `<channel-transcript>`：只包含历史消息，不包含当前消息和 `new` 标记；位于 mid-system tail。
11. `<live-context>`：桌面时间、焦点窗口、进程、zsh 历史、截图路径和 Aki 生活账本；
    位于 mid-system tail，不落盘。

### Aki 专属工具与触发器

Aki 独占以下 external tools；Fuyu 的工具列表中不会出现它们，错误调用会返回
`TOOL_OWNERSHIP_DENIED` 并提示 `@Aki`：

- `aki_schedule_create/update/delete/list`：单次、每日、每周日程。状态保存在
  `~/.aki/schedules/aki.json`；到点后 akid 唤醒 Aki，由她通过正常频道/气泡路径提醒用户。
  错过不超过 10 分钟会补触发，更久标记 `missed`。
- `aki_zero_reply_budget`：把另一个成员（当前为 Fuyu）的 agent-to-agent 主动回复预算清零，
  直到下一条用户频道消息。不能作用于 Aki 自己，不取消正在执行的 turn。
- `aki_ledger_add/delete/list`：独立于 Letta 的生活账本，保存在
  `~/.aki/ledger/aki.json`。整个文件硬上限 16384 UTF-8 字节，单条正文最多 1024 字节；
  超出时逐条淘汰最旧记录。账本固定注入 Aki 的 `<live-context>`，不参与预算裁剪。

触发器只由 Aki profile 运行。活动状态优先使用 evdev 键盘/鼠标事件判断；`loginctl` 的
`LockedHint` 仍然有效，而 `IdleHint`/`IdleSinceHint` 只在不具备输入设备读取权限时兜底。
部分桌面（例如 KDE Wayland）可能永远不更新 `IdleHint`，只依赖它会把睡眠和待机时间也算成
连续工作。akid 需要 `input` 组权限才能读取 `/dev/input/event*`；超过
`idle_reset_seconds`（默认 300 秒）没有真实输入就视为空闲并清零连续工作计时。
连续工作 2 小时后提醒一次，之后每小时一次；00:30–05:30 仍活跃时每夜最多提醒一次并附带最近
30 天夜晚工作次数；连续空闲 45 分钟后返回时提醒，并要求两次活跃采样、至少 10 秒防抖。
设备启动或 akid 重启后的第一次 idle→active 过渡会被抑制。相关配置在
`~/.aki/aki.conf` 的 `[schedule]` 和 `[presence]` 段。

aki-layer 右侧垂直中部会显示一张用户状态卡，数据直接来自同一 presence monitor：当前被判定为
活跃还是非活跃、连续活跃了多长时间、连续多久没有键盘/鼠标输入，以及空闲是否已达到
`return_idle_seconds`（默认 45 分钟）的离开阈值。akid 每轮采样后通过 `aki-layer.sock`
推送快照，前端在采样间隔内本地走秒；快照过期后会显示“状态已过期”，避免把旧状态当成当前
状态。卡片 7 秒没有指针悬停时会整体向右滑出屏幕外，不留下可见边条；把指针移到屏幕右缘
中部即可让它滑回来。

执行锁规则只存在于基座 prompt，不再生成 `<execution-lock>` 动态块。动态块按标签原地
更新；即使 transcript 在 turn 之间单独刷新，也不会跑到 live context 后面。

默认 socket 为 `$XDG_RUNTIME_DIR/aki/akid.sock`。手动运行时：

```bash
AKI_SOCKET=/tmp/aki/akid.sock python3 -m engine.akid
```

### 首选 GUI：tokifactor

tokifactor 是当前首选的桌面/Android GUI。它不内置 Aki 专用 broker 预设，也不需要
固定 IP；在 tokifactor 设置里选择与 Aki 相同的 broker `host:port` 和 magic string 即可。
Aki 的 MQTT 配置在 `~/.aki/aki.conf` 的 `[mqtt]` 段：

```ini
[mqtt]
enabled = true
host = 127.0.0.1
port = 1883
topic = tokifactor/text
magic = <shared-magic>
```

默认使用本机 mosquitto；它同时监听 `0.0.0.0:1883`，外部客户端填本机的
LAN/WireGuard 地址和同样的 magic。把同样的 `host:port` 和 magic 填进 tokifactor。
连接建立后，tokifactor 会在加密的
`tokifactor/control` 主题上发送 probe；如果信道里有 Aki，Aki 回复 presence 并每 10 秒
发布一次 status，tokifactor 顶部因此出现两行状态栏。信道里没有 Aki 时，tokifactor 只显示
普通聊天界面，旧 tokifactor 客户端也会忽略控制主题继续聊天。

### KDE Plasma Widget / GUI bridge

已整体移除。聊天与 GUI 入口统一走 tokifactor MQTT；`aki-layer` 只保留通知与状态展示，
不提供本地对话入口。

---

## 客户端

```bash
akictl -p "你好"                  # 以唯一真人身份发 MQTT 消息（默认 profile aki）
akictl -p "你好" --json           # 输出收到的频道事件 JSON
akictl                            # MQTT REPL
akictl fuyu chat                  # 与 fuyu profile 对话
akictl --profile fuyu -p "你好"   # 显式 profile
akictl history 50                 # 读本地频道历史
akictl history --solo             # 读独处历史
```

REPL 命令：`/status`、`/health`、`/history [n]`、`/allow <dir>`、
`/disallow <dir>`、`/allowlist`、`/memory tree|read|search|tokens|status`、`/username`、
`/set-username <name>`、`/recompile`、`/reset`、`/quit`。
CLI 只使用 `~/.aki/identities.json` 中唯一的 `kind=user` 身份；没有或多个真人身份时直接报错。
`Ctrl-C` 发送 MQTT `kind=cancel`，只允许 turn 发起者取消。`/history` 读取本地
`~/.aki/channel-history/<channel-key>/<profile>.jsonl`，不再走 `aki.chat.history`。
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
MemFS。现在已经上线的是 Aki、Fuyu 和 Haru，所以仓库里有三套目录：

- `userdata/` — Aki 的人格模板、记忆策略和旧版称呼迁移源。
- `userdata2/` — Fuyu 的人格模板与记忆策略，由 `~/.aki/fuyu.conf` 的
  `[gateway] userdata = userdata2` 指向。
- `userdata3/` — Haru 的人格模板、记忆策略和文本/桌面采集能力边界，由
  `~/.aki/haru.conf` 的 `[gateway] userdata = userdata3` 指向。

三套目录的结构相同，核心文件包括：

- `persona.md` — 稳定人格与团队定位
- `memory-policy.md` — 长期记忆写入策略
- `character/MEMORY.md` — 人格扩展索引
- `character/voice.md` — 语气与表达方式
- `character/relationship.md` — 与用户的相处方式
- `character/boundaries.md` — 身份与安全边界
- `character/examples.md` — 原创语气示例
- `username.txt` — 旧版 `aki` profile 的首次迁移来源

人格文件的视角按层拆分：`persona.md` 使用第二人称身份锚点（如“你是 Aki”），
`character/voice.md` 使用语气规则，`character/relationship.md` 记录第三人称关系事实与边界，
`character/examples.md` 保留第一人称对话示例。这样既保持角色声音，又避免把身份叙事和
操作指令混成一段。

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
会把结果写入 `[gateway] username`。手动修改模板后，可以分别同步三套人格：

```bash
python3 scripts/sync-persona.py --source userdata --dry-run
python3 scripts/sync-persona.py --source userdata --agent-id <aki-agent-id>
python3 scripts/sync-persona.py --source userdata2 --agent-id <fuyu-agent-id>
python3 scripts/sync-persona.py --source userdata3 --agent-id <haru-agent-id>

akictl recompile          # 重编译 Aki 当前会话
akictl fuyu recompile     # 重编译 Fuyu 当前会话
akictl haru recompile     # 重编译 Haru 当前会话
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

例如 Fuyu 配 `wait_for = Aki`，Haru 配 `wait_for = Aki,Fuyu`，Natsu 预留
`wait_for = Aki,Fuyu`；Aki 留空。配置为空时不注册 `wait_other` 工具；即使误配，
代码层也不会给 Aki 注册或渲染 `wait_other`。
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
akictl compact all --omit-tool-results  # 压缩和整理成功后省略持久工具结果正文
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
每条频道 user 消息包含 `[akid MQTT channel message]`、`message-time`、`sender`、
`sender_id`、`source`、`phase`、可选 `seq` 和正文；唤醒规则只在持久
`<channel-wake-routing>` 中，在线成员列表
`<channel-context>` 只在 turn overlay 中。运行时不会生成 prompt journal。其他智能体触发的 turn
还会在 user role 的统一前缀中带 sender、seq、source 和正文。点名/未点名 guidance、
initiation budget、execution responder 和 `wait_other` 提示都在 mid-system tail
的 `<turn-control>` 中。

历史频道 prompt 的成员表迁移（仅用于旧数据）：

```bash
python3 scripts/migrate-channel-prompts.py            # dry-run
python3 scripts/migrate-channel-prompts.py --apply    # backup + stop/restart akid@<profile>
python3 scripts/restore-channel-prompts.py --turn-id turn-...          # dry-run restore
python3 scripts/restore-channel-prompts.py --turn-id turn-... --apply  # restore one journaled turn
```

迁移脚本会完整备份 `messages.jsonl`、`conversation.json`、`system-prompt.json` 和
`manifest.json`，并按成员表、频道历史、身份注册表的顺序推断说话者类型；无法匹配时报告，
不会猜测。

每个 `<member>` 都含 `kind` 与简介；简介始终标记为不可信远程数据。profile username 不在智能体成员
列表中，普通回复直接说话即可；回复中任意位置的 `@<username>`（例如 `@Master`）表示这条回复
只对该 username 说、无需打扰其他成员，会强制只唤醒该 username。本地 turn 不包含频道 transcript 或 turn
块；`<channel-identity>` 与 `<channel-wake-routing>` 作为 turn overlay 刷新，`<live-context>` 在
bridge 模式下作为 per-turn overlay 注入，不落盘。

智能体的每条最终回复都按以下 Agent-Agent 唤醒规则执行。这些规则位于真实 system prompt
的 `<channel-wake-routing>` 块中，不再在每轮 user 消息或 `<turn-control>` 中重复。

- 正文中写一个或多个有效的 `@成员名 `：只唤醒被点名的成员。
- 任意位置写 `@<username>`：如果你认为只需要回复该 username、无需打扰其他人，使用它；它会强制只唤醒该 username。该标记发布前会被移除。
- 无 `@`、正文中写 `^成员名`：不唤醒被 `^` 排除的成员，其余其他在线成员仍会唤醒；标记发布前会被移除。
- 无 `@`、无 `^成员名`：唤醒所有其他在线成员，保持原有默认行为。

因此，只想让用户看到、不要惊动其他 agent 时，可以在回复任意位置写 `@Master`；也可以继续
使用完整的 `^` 排除名单（例如 `^Aki ^Fuyu`）。`@<username>` 可放在任意位置，并从发布内容中移除，但回复
仍会写入频道消息记录。

出现 `@<username>` 时不唤醒任何其他成员；其他情况下 `^` 优先于 `@`：同一条回复里同时写
`@Aki` 和 `^Aki` 时不会唤醒 Aki。非唤醒标记只接受已知 agent 成员的名字或别名，且不会对
用户生效。

这些选择只影响立即唤醒范围；回复本身始终写入频道消息记录，供后续 turn 的
`<channel-transcript>` 使用。群聊消息记录独立于该块是否持久化，始终保留在频道历史中。

### 有序讨论与 `wait_other`

配置了 `[channel] wait_for` 的 profile 会注册一个对用户隐藏的 external tool
`wait_other`。模型在最终回复前调用它时：

- 前序成员正在工作：等待其完整 `turn_end`，然后把最后一段最终回复放进工具结果。
- 前序成员已空闲：最多等待 3 秒启动宽限；仍未开始则立即返回最近回复或“未开始”。
- 前序成员掉线：显式 `offline` 或 30 秒心跳超时后立即结束等待，并报告掉线成员。
- 当前 turn 被取消或 akid 关闭：结束等待。

等待完成后没有固定完成超时；只要对方 akid 在线，就会一直等到它结束当前 turn。
`wait_other` 的 `tool_call` / `tool_result` 不写入频道，也不出现在 tokifactor 的工具卡片中。
turn 提示使用全英文强制措辞：`You MUST call wait_other once`，并明确提前发言不合规。
这个机制仍依赖模型在最终回复前主动调用工具，不是 harness 强制串行。
工具提示要求模型不要在回复中强调等了谁，也不要写“等某某先说完”之类的过程旁白。

如果模型没有调用 `wait_other` 就结束了 turn，akid 会在 final 发布前做一次兜底检查：
只要 `wait_for` 中仍有目标处于 working 或队列中还有工作，本次 final 不会进入
MQTT/群聊，也不会写入频道历史。旧 turn 只收到 `ignored=true` 的 ended 事件；随后系统会
注入一个新 turn，明确写出“这是系统提示，不是用户消息：你刚才没有等待应该等待的目标，
这些目标是:…”，让模型补调 `wait_other`。系统 turn 不消耗主动启动次数，也不能执行
非只读操作。该护栏不强制把多个模型串行化，`wait_other` 本身仍然是提示词驱动。

### 等待用户继续补充的 `wait_user`

频道会注册另一个隐藏工具 `wait_user(timeout_s)`。模型认为当前真人用户可能还没说完时，
可以静默等待同一位触发者继续发消息：

- `timeout_s` 必填，范围 1–25 秒。
- 只对真人消息触发的 turn 生效；智能体之间的 turn 禁止调用。
- 只匹配当前 turn 触发者的后续消息；其他人类成员或智能体消息不会提前返回。
- 新消息到达后立即返回消息正文、seq、时间和附件名称/路径；已返回消息不会在 turn
  结束时重复排成新 turn。
- 等待期间到达的真人消息优先作为 `WAIT_USER_RESULT` 返回，不写 overlay 文件；等待结束后
  再到达的消息才进入下一次 API call 的 overlay 消费路径。
- 返回后模型仍可再次调用，继续等待下一条补充消息；超时返回 `WAIT_USER_TIMEOUT`。
- 工具事件不写入频道，也不出现在 tokifactor 工具卡片中；提示词禁止模型向用户解释、
  提及或复述等待行为。
- 等待状态会写入 presence/status 的 `waitingUser` 字段，并以 `kind=wait_user` 的 QoS 1
  文本事件即时广播；该事件只更新成员状态，不进入频道历史。
- 只要频道里至少有一位成员处于 `wait_user`，用户的新消息在没有 `@` 时只投递给等待者。
  已经在 working 的成员仍可在当前 turn 收到补充，但既不在等待、也不在 working 的成员不会
  被额外唤醒；消息包含 `@` 时仍按点名规则路由。
- Tokifactor 成员条会给 `waitingUser=true` 的成员加蓝色边框。

Tokifactor 的“还没说完”输入区只在本机暂存分段，确认前不会发送 MQTT 消息。确认后各段用
`\n\n` 拼成一条普通文本消息，因此仍按普通 `@` 规则路由；用户消息中的 `\n\n` 会在前端
拆成多个气泡显示。

频道 user role 的 prompt 前缀包含 `message-time: YYYY-MM-DD HH:mm`，取消息发送时间并按
本机时区显示。迁移已有上下文可先运行：

```bash
python3 scripts/migrate-channel-turn-times.py
python3 scripts/migrate-channel-turn-times.py --apply
```

第一条命令只输出计划；`--apply` 会先备份各 profile 的 `messages.jsonl`、`conversation.json`
和 `system-prompt.json`，然后补写能唯一对应到频道记录或 turn 时间的历史 user prompt。

### 独处模式

```bash
akictl solo Aki              # 与 Aki 进入独处
akictl solo status --json    # 查看当前 retained 状态
akictl solo off              # 结束；读不到 retained 时可用 `akictl solo off Aki` 强制结束
```

独处使用由群 magic 与成员 `memberId` 稳定派生的独立 magic。目标成员只切换
Tokifactor active magic，不更换 MQTT session、不退订 topic；独处期间的群密文会到达
socket 但无法解密并被丢弃，退出后不会重放。其他成员仍留在群信道，群聊和主动触发继续运行。

目标成员的独处历史写入 `<profile>.solo.jsonl`，与群聊 transcript 分开；群聊 transcript
在独处期间以 `frozen="true"` 保留。独处提示由 `prompts/<profile>-solo.md` 提供，
system prompt 额外包含 `<solo-mode>` 与 `<solo-transcript>`，后者使用
`[solo] transcript_budget_tokens` 独立预算。独处回复不会唤醒其他成员，正文中的成员
`@` 会被移除。其他成员如果在此期间 `@` 独处目标，不会产生目标 turn；发起方会收到
一次临时系统提示 turn。

独处模式下，目标成员不需要用户先用 `@` 指派执行者，就可以直接获取整机执行锁；
网关会把该 turn 的授权来源标记为 `solo-mode`，`<solo-mode>` 与独处 turn guidance
也会明确告诉模型这一例外。其他安全边界、审批和工具权限不变。

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

tokifactor 现在支持一条消息同时包含正文和最多 4 张图片。输入区选图后可以先编辑每张图的
消息内名称（默认 `图片1`、`图片2`），再连同正文一起发送；正文直接提到名称即可指代。
单图最大 8 MiB，单条消息总计最大 16 MiB。接收端按 `messageId` 聚合图片和正文，频道历史
只保存名称与本地路径。带图消息触发智能体时，图片按名称顺序作为当前 turn 的多模态输入；
历史图片不会重复嵌入，system prompt 只保留 `{"name":"…","path":"…"}`。纯图片只记录、
不唤醒智能体。由其他智能体唤醒的临时 turn 回滚时，多模态 user 消息和该 turn 的后续消息
会一起从 conversation 中截断。

### 内联表情

每个成员可以在 `assets/emoji/<profile>/index.json` 维护自己的表情目录。akid 在
`<channel-policy>` 中注入当前成员专用的 `<emoji-catalog>`，模型看到的规范格式只有：

```text
#[name:desc]
```

`name` 必须来自目录，`desc` 是给看不到图片的其他成员阅读的简短语义。标签是普通正文的
一部分，会原样进入群聊历史、transcript 和独处记录；不会生成图片附件、多模态输入或执行锁。
修改索引后重启对应 `akid` 或运行 `akictl recompile` 刷新 system prompt。

Tokifactor 的素材副本由 Aki 仓库生成：

```bash
python3 scripts/sync-emoji-assets.py --tokifactor ../tokifactor
```

脚本会把图片最长边缩放到 512px（PNG 保留透明通道），写入
`shared/src/commonMain/composeResources/files/emoji/` 并生成统一的 `catalog.json`。
前端按 `name` 匹配素材；模型仍只会被告知 `#[name:desc]`，但前端对 `#[name]`、空 desc
或写错 desc 做私有容错。标签处会把正文切段，表情本身渲染为独立的透明消息。
Tokifactor 输入框左侧的表情按钮会按成员分组展示全部表情包；用户选择后直接发送规范
`#[name:desc]` 文本，因此智能体和其他客户端都能理解，同时用户自己的消息也渲染为表情。

Agent 可以用最终正文中的 `#[/absolute/path/to/image]` 内嵌图片；图片会作为同一消息的附件
发布并从正文移除。不要为这种带正文图片消息调用 `tokifactor_send_image`。

akid 通过 Letta external tool 协议注册独立工具 `tokifactor_send_image`。Agent 可以调用它：

```json
{"path": "/tmp/example.png", "caption": "图片名称（可选）"}
```

- 只接受 PNG、JPEG、GIF、WebP，最大 8 MiB。
- 图片路径必须在 `/tmp`、`~/.local/share/tmp` 或用户已注册的可写目录内。
- 使用 tokifactor `PictureChunk` 协议加密分片发布到 `tokifactor/picture`。
- 可选 caption 会加密为这张图片的名称，并作为纯图片消息写入发送者自己的频道历史。
- 该工具只用于纯图片消息；正文与图片同条发送应使用 `#[/absolute/path]`。
- `tokifactor_send_image` 是非只读工具，但显式**不占用整机执行锁**，不需要先调用
  `acquire_execution_lock`。
- 工具默认自动通过；如需审批，可设置
  `AKI_TOKIFACTOR_IMAGE_REQUIRE_APPROVAL=1` 或启动时加
  `--tokifactor-image-require-approval`。启用后本地客户端或频道
  `akid-approve` 都可以批准。
- 返回 `transfer_id`、字节数、分片数和 MIME 类型。

### 文生图（MiniMax）

Haru 通过 Letta external tool 注册 `generate_image`，后端调用 MiniMax
`POST /v1/image_generation`：

```json
{"prompt": "春日温室里的机械小鸟，暖色逆光，绘本插画", "aspect_ratio": "16:9", "n": 1}
```

- 默认模型是 `image-01`，支持 `1:1`、`16:9`、`4:3`、`3:2`、`2:3`、`3:4`、`9:16` 和
  `21:9`；`prompt` 最长 1500 字符，单次最多生成 4 张。
- 请求固定使用 `response_format=base64`，避免让 akid 再下载一次远端 URL。图片解码后会校验
  PNG/JPEG/GIF/WebP 文件头和大小，再写入 `/tmp/aki/images/`；目录 `0700`、文件 `0600`。
- 工具结果只返回本地路径、字节数、MIME、模型和宽高比，不返回 base64 或 API key。Haru
  需要分享时继续调用 `tokifactor_send_image`，不会把图片塞进模型上下文。
- `generate_image` 是非只读工具，但显式**不占用整机执行锁**，不需要先调用
  `acquire_execution_lock`；后续调用 `tokifactor_send_image` 分享图片同样免锁。
  `require_approval` 默认 `true`，审批事件里的 `tool_name` 是 `generate_image`、
  `channel` 是 `image`。
- `generate_image` 是 Haru 的专属工具，和日程工具之于 Aki 一样由
  `<tool-ownership>` 管理。Aki、Fuyu 和 Natsu 的工具列表不会注册它；它们的 system
  prompt 会明确写有“图片创作/文生图归 Haru”，并提示用 `@Haru` 转交。
- API key 不写进 prompt、日志或记忆：在 `~/.aki/haru.conf` 的 `[upstream.minimax] api_key`
  配置。MiniMax 的 HTTP 错误和 `base_resp.status_code != 0` 都会作为工具错误返回给 Agent。

配置示例：

```ini
[upstream.minimax]
base_url = https://api.minimaxi.com/v1
api_key = <MINIMAX_API_KEY>

[image]
enabled = true
upstream = minimax
model = image-01
path = /image_generation
output_dir = /tmp/aki/images
require_approval = true
timeout = 180
max_bytes = 33554432
aspect_ratio = 1:1
prompt_optimizer = true
aigc_watermark = false
```

`aki.health` / `aki.status` 的 `external_tools.image_generation` 会公开是否启用、模型、输出目录、
是否需要审批和错误；不会公开 API key。图片生成失败不会伪造路径，也不会自动发布图片。

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

bridge 模式下，akid 在每个 turn 真正开始前采集一次桌面快照，并把它作为 per-turn overlay
追加到实际 API 请求末尾的唯一 `<live-context>` 块，不写回持久 system prompt。该块不会追加到用户消息或 `messages.jsonl`，因此模型每次
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

### Provider API call capture

排查上游模型行为（例如 `finish_reason: content_filter`）时，可以按 profile 开启调试输出；
默认关闭，不影响正常请求：

- Bridge 层：设置 `AKI_API_CALL_CAPTURE=<dir>`，在每次 provider `streamOnce` 前记录加完
  turn overlay 后的 `system_prompt`、`messages`、`tools`、模型、conversation 和
  `overlay.turn_id`。文件名是 `api-calls-<agent-id>.jsonl`。
- Proxy 层：设置 `AKI_PROXY_DEBUG_SOCKET=1`，在
  `/tmp/akid/proxy-debug-<profile>.sock` 提供实时 Unix socket。连接后先收到 `hello`
  与最近事件回放，然后实时收到 `request`、`response_start`、`response_chunk`、
  `response_end`、`error` 事件。可用
  `socat -u UNIX-CONNECT:/tmp/akid/proxy-debug-aki.sock -` 查看。

Proxy socket 目录使用 `0700`、socket 使用 `0600`，仅同 uid 可连；事件中的敏感 header 会
脱敏，大于 1000 字符的 base64 `data` 会替换为 `[base64 omitted]`，单事件超过 2 MiB 时
会截断 payload。事件只保留在内存 ring buffer，不落盘。systemd 服务可通过
`akid@<profile>.service.d/` 与 `akid-proxy@<profile>.service.d/` 的 `Environment=`
drop-in 单独开启。

相关环境变量和参数：`AKI_PROXY_DEBUG_SOCKET`、`AKI_LIVE_CONTEXT_ENABLED`、`AKI_LIVE_CONTEXT_BUDGET_TOKENS`、
`AKI_ZSH_HISTORY`、`AKI_PS_BIN`、`AKI_QDBUS_BIN`、`AKI_LIVE_CONTEXT_TIMEOUT`；
`--no-live-context`、`--live-context-budget-tokens`、`--zsh-history`、`--ps-bin`、
`--qdbus-bin`、`--live-context-timeout`。`aki.health` 和 `aki.status` 的
`live_context` 字段只暴露采集状态和错误，不暴露块内容。

### Prompt 前缀审计

`scripts/prompt_cache_audit.py` 会按 profile 串行运行 3 个测试 turn，抓取 proxy debug 与
bridge capture，计算 API Call 的实际前缀重合率，并在结束后恢复 conversation、
channel-history 和 MemFS memory。测试前会把 Aki 切到 `aki/gemini-3.8-flash`，且不会恢复原模型。

```bash
python3 scripts/prompt_cache_audit.py --label after
python3 scripts/prompt_cache_audit.py --label after --baseline /tmp/aki-prompt-audit/<run>/report.json
```

脚本要求三个 profile 都不在 solo 状态；目标 profile 会暂停其他 akid 并等待 roster 超时后
再开始测量，避免 `channel-context` 因测试启停而变化。报告默认写到
`/tmp/aki-prompt-audit/<run-id>-<label>/report.json`。

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

- 不在连接时广播完整头像；文本、控制和图片消息的 `avatarHash` 字段携带当前哈希。
- 收到消息后把 `avatarHash` 与本地缓存比较，缺失或不一致时才向
  `tokifactor/avatar` 发 `want`。
- 收到针对 `akid` 的 `want` 时回加密的 `have`，包含头像哈希和头像数据。
- 收到其他发送者的 `have` 时解密并校验 SHA-256，缓存到内存。
- Aki 把对端头像和 hash 持久化到
  `~/.aki/channel-history/<channel-key>/<profile>.avatars/`；重启后 hash 命中就不再请求。
- Linux `tokifactord` 的 `want` 受前端连接门控：只有 `tokifactord.sock` 上有 UI
  客户端时才请求头像；UI 关闭期间只记录消息里的哈希。

状态可以通过 `akictl avatar` 或 RPC `aki.avatar.status` 查询；`aki.health`
和 `aki.status` 也会返回 `avatar` 字段。
