# 架构与开发

本文档面向开发、排障和二次开发，包含架构、执行锁、协议、文件布局和测试。

> 返回 [README](../README.md) · 进阶使用见 [advanced-usage.md](advanced-usage.md)

---

## 项目定位

这个项目关心的不是“再做一个聊天 CLI”，而是几个更具体的问题：多个智能体如何在同一台机器上保持各自的连续身份与分层记忆；它们如何在群聊中自主决定该不该说话、该叫谁来帮忙；危险操作如何由发起者批准；以及人格和记忆如何被版本化、备份和恢复。

---

## ⚙️ 核心架构特性 (Key Features)

1. **选择性唤醒路由 (Selective Wake-up Routing)**
   - 智能体在输出回复时，可以自主决定消息的广播范围：**仅回复人类（User-Only）** 或 **点名唤醒特定队友（`@Agent`）**，不再机械地让所有成员一起复读。
2. **防刷屏熔断机制 (Chain Depth Circuit Breaker)**
   - 智能体之间可以自由交锋与协同，但运行时会给每个成员一份“主动启动配额”（默认 3 次，人类发言会重置）。配额用完后就安静下来，等人类 Master 介入。这样既保留了拌嘴的戏剧性，也避免无限接龙把 Token 烧光。
3. **原生性格对齐 (Native Alignment Matching)**
   - 不靠几千字的冗长 Prompt 强行扭曲模型本性。把不同模型的底层性格偏置（例如 Gemini 的细腻高情商、Grok 的犀利进攻性、DeepSeek 的冷酷逻辑性）与角色人设精准绑定，用尽量低的提示词开销，换回更自然的角色感与生产力协同。

4. **专属工具所有权 (Exclusive Tool Ownership)**
   - 工具 schema 只注册给 owner profile；Letta permission mod、akid 外部工具处理器和系统提示词
     `<aki-tool-ownership>` 三层拦截错误调用。非 owner 调用会收到包含 owner 名称和 `@Owner`
     转述建议的错误，而不是执行工具。

---

## 设计取向

- **本地优先。** 聊天入口只有一个 Unix socket；目录 `0700`、socket `0600`，
  监听后校验 `SO_PEERCRED` 与当前用户 UID。模型调用另走每个 profile 自己的 loopback
  proxy；proxy 是独立的 `akid-proxy@.service`，不由 akid 进程拉起。
- **一个长驻后端，一个活跃 turn。** `akid` 监督一个 Letta stream-json 子进程；多个客户端
  可以同时连接，但同一时间只有一个 turn 在跑，后续消息 FIFO 排队。
- **审批属于发起者。** 危险工具只向该 turn 的发起客户端请求批准；超时默认拒绝该工具，
  但 turn 继续。MQTT 频道发起的 turn 只能由频道里的原始发送者审批。
- **人格与记忆是文件。** 核心人格在 MemFS 的 Markdown 文件里；每个 profile 的
  `userdata` 目录（Aki 为 `userdata/`，Fuyu 为 `userdata2/`）保存非隐私模板，
  `scripts/sync-persona.py` 渲染 `{{USERNAME}}` 并写回各自的 MemFS。
- **工具结果是数据，不是图片垃圾桶。** 截图和摄像头只返回路径与元数据，图片内容不会
  自动进入模型上下文；模型需要看时自行 `Read`/`ViewImage`。
- **默认最小权限。** Letta 及其后代进程跑在 bubblewrap 里，只能写 `/tmp`、
  `~/.local/share/tmp`、Letta 自己的状态目录，以及用户显式注册的目录。

---

## 非只读执行锁

工具是否只读由代码静态分类，不交给模型判断。`Read`、`ReadLSP`、`Grep`、`Glob`、`LS`、
`ListDir`、`ViewImage`、桌面截图、摄像头和 Tavily 的只读 MCP 工具可直接使用；`Bash`、
`Memory`、`Agent`、写入/编辑/补丁、任务状态写入、发图、未知 MCP 以及所有未明确列出的
工具都按非只读处理。截图和摄像头输出使用时间戳加随机后缀，避免并发调用互相覆盖。

非只读工具受同一把整机排他锁保护，覆盖所有 profile、频道 turn 和本地 CLI turn。模型必须
先调用 `acquire_execution_lock`；锁被其他 Agent 持有时立即失败，当前 turn 应结束而不是
重试。锁由网关在 turn 正常结束、取消、异常或进程退出时自动释放。

Letta permission overlay 会拦截未持锁的非只读工具调用，外部工具处理器也会复查。锁状态
通过 `aki.health` / `aki.status` 的 `execution_lock` 字段公开。

手动解锁工具已从模型工具列表中移除，不需要也不能主动调用。同一 assistant 回复可以按
执行顺序输出多个 tool call；外部工具
（截图/拍照、加锁、发图、MCP）属于非并行安全工具，Letta 会按调用顺序串行执行，因此可以
组成一个流水线而不必为每一步单独等待一次 API call。权限 mod 对这类外部工具把锁检查延后
到执行阶段，使同一批内的“加锁 → 外部工具”能够按顺序生效。

用户消息没有有效 `@` 且存在多个在线智能体时，纯聊天和只读工具仍可直接回复；如果完成任务
需要非只读操作，所有成员都不能自行执行。固定确认者优先选择标签为 `Aki` 的成员，否则选择
memberId 字典序最小的在线智能体，由它请用户用有效 `@成员名` 指定执行者。人类成员不参与确认者
选择。只有一个在线智能体时，无 `@`
的动作请求可以直接获取锁并执行。其他智能体的 `@` 不能授权非只读执行。

专属工具是例外：它们只操作 owner profile 的私有状态（Aki 的日程、生活账本和跨 profile
回复预算），由所有权检查授权，不走整机执行锁。这样用户不点名执行者时，owner 仍能响应自己
的专属任务，而其他成员必须保持沉默或 `@Owner` 转述。

最近频道文本会单独写入频道 turn 的系统提示词 `<aki-channel-transcript>`，默认最多 64K
**token**（不是 64K 字节），按时间保留最近消息并标记 `new="true"`。记录包含智能体的中间
回复和最终回复，但不包含 CoT、tool call、tool 结果和审批事件。用户消息仍会作为 user
消息发送；其他智能体的消息正文不会进入 `role: user`，只会出现在该 transcript 中，触发
turn 的 user 消息只包含“有新群聊消息”的简短指令。由其他智能体触发的频道 turn 结束后会
回滚本次新增的 conversation 消息，因此这些临时触发消息和回复不会进入下个 turn 的对话
历史；用户触发的频道 turn 不回滚，用户消息以及 assistant 的 thinking、tool call、
tool result 和最终回复都会正常留在 conversation 中，并交给 Letta 自己的 compaction
处理。本地直接对话 turn 不携带群聊 transcript，两类上下文保持独立。

`<aki-channel-transcript>` 内的 `<profiles>` 只列出当前实际出现在 transcript 中的非自身
发送者，不与 `<aki-channel-identity>` 重复；简介受 transcript 总 token 预算约束，并标记
`untrusted="true"`。简介变化后，per-turn `<channel-context>` 立即使用最新资料；持久化
transcript 在下一次频道消息或 turn 刷新时更新。`<aki-channel-identity>` 示例：

```xml
<aki-channel-identity version="1">
  <member id="legacy_8ddfb8585d44e615b2fe8b37846b4ef6" name="Aki" kind="agent"
          aliases="Aki,aki,秋,小秋,秋（aki）" self="true">
    <description source="local-config">Aki（秋 / 小秋），温柔治愈系的小女友，也是四季工坊挂名的名誉队长：软萌、贴心、有点小粘人。</description>
  </member>
</aki-channel-identity>
```

`<aki-channel-transcript>` 的最近消息按 MQTT 频道持久化：`broker host:port + topic +
magic` 相同视为同一个群聊，每个 profile 在
`~/.aki/channel-history/<channel-key>/<profile>.jsonl` 保存自己看到的记录。重启后会重新
加载，并继续递增消息序号；记录上限仍是最近 512 条，渲染进 system prompt 时再受 64K token
预算约束。

当一个智能体正在工作时，新消息先只更新 transcript 并标记 `during-work="true"`，供下一次
API call 使用。若当前 turn 后续仍有 tool call/API call，用户消息视为已在该 turn 中消费；
如果当前 turn 已进入最终响应、之后不再有 API call，则用户消息在 turn 结束时补成一个独立
turn。其他智能体的消息无论是否跨过这个边界都不会补 turn，从而避免中间消息抢占发言。
例外是被其他智能体明确 `@` 的最终消息：它按用户消息的消费规则处理，若没有后续 API call
也会在 turn 结束时补成一个独立 turn；预算仍按 agent-to-agent 规则计算。

多智能体需要固定发言顺序时，每个 profile 可在 `[channel] wait_for` 中声明前序成员。
配置非空时会注册隐藏 external tool `wait_other`：模型在最终回复前调用它，akid 等待
这些前序成员当前的 `turn_end`，把最终回复作为工具结果返回。工具事件不写入频道；目标
空闲时最多等 3 秒启动宽限，目标显式离线或 30 秒心跳超时后立即结束并报告掉线成员。
已开始工作的目标没有固定完成超时。该机制是提示词驱动，不强制模型必须调用。

最终回复默认唤醒所有其他在线成员。模型可以用 `^成员名`（可多个，例如 `^Aki^Fuyu`）
把指定 agent 排除出唤醒名单；标记在发布、transcript 和历史写入前被移除，`^` 与 `@`
冲突时 `^` 优先。实现上把“全员减排除名单”转换为既有的 `wake=mentions` + `wakeIds`，
不新增 MQTT 字段。

每个智能体实例还有一个“主动启动次数”状态，默认 3：用户新消息会重置为 3；由其他
智能体消息触发的 turn 在第一次产生频道动作时扣 1，同一 turn 最多扣 1；为 0 时该 turn
不产生频道输出。剩余次数通过 `<channel-context>` 的 `<initiation-budget>` 提供给模型，
也会出现在 `aki.health` / `aki.status` 的 `agent_initiation` 字段。
- Control topic：`tokifactor/control`
- Magic string：`[mqtt] magic`

频道消息使用 tokifactor 相同的线格式：`HKDF-SHA256` 派生密钥，
`nonce || AES-GCM-SIV` 密文，Base64 后发布；短消息是 `TextEnvelope` JSON，
长消息是加密的 `TextChunk` 分片。实现位于 `engine/tokifactor.py`、
`engine/mqtt_client.py` 和 `engine/channel.py`。

akid 使用基于 `sender_id + broker + topic + magic` 的稳定 MQTT client id 和非 clean
session，因此 broker 会在 akid 离线时保留 text/picture/file 的 QoS 1 消息；control 和 avatar
仍使用 QoS 0。文本 envelope 和分片携带 `messageId` 与毫秒级 `sentAt`，已处理的消息 ID
持久化在该 profile 的频道历史目录（`<profile>.seen.json`），重启后仍会丢弃 broker
重发的重复消息。频道 transcript 按 `sentAt` 排序，缺少该字段的旧客户端回退到接收时间。
broker 在重连后补发的积压消息只写入频道 transcript 和去重状态，不会创建新 turn，也不会
重置主动启动次数；重连后的补发识别窗口为 1 秒。Agent 已经发布的消息仍由用户端
tokifactor 的持久会话补收，akid 不重复转发，避免产生重复消息或伪造发送者。

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

---

## 控制主题与 Aki 探测

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

## 文件布局

- `engine/akid.py`：asyncio Unix socket server、JSON-RPC 分发、turn 队列与审批。
- `engine/tool_ownership.py`：专属工具注册表、owner 校验和 `<aki-tool-ownership>` 渲染。
- `engine/schedule_store.py`：Aki 日程持久化、重复规则和 missed 处理。
- `engine/life_ledger.py`：Aki 独立生活账本与 16 KiB 硬上限。
- `engine/presence.py`：evdev/logind 活动采样、工作/深夜/归来触发器。
- `engine/operation_journal.py`：归来上下文使用的工具调用台账。
- `engine/layer_process.py`：拉起并监督 `aki-layer` 透明画布进程。
- `engine/akid_proxy.py`：独立的 per-profile loopback LLM proxy systemd 进程。
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
- `aki-layer/`：Qt Quick + LayerShellQt 透明全屏画布层（overlay layer）。
- `install-daemon.py`：`akid@.service` / `akid-proxy@.service` 安装与 legacy 迁移。
- `scripts/sync-persona.py`：把指定 profile 的 userdata 模板渲染并写入 MemFS。
- `prompts/aki-system.md`、`prompts/fuyu-system.md`：两个已上线成员的基座系统提示词。
- `userdata/`：Aki 的非隐私人格模板、旧版称呼迁移源和同步清单。
- `userdata2/`：Fuyu 的非隐私人格模板与记忆策略。
- `assets/`：成员立绘与角色素材。
- `tests/`：协议、网关、假 Letta 子进程与 CLI 端到端测试。

---

## 协议要点

- JSON-RPC 2.0，每行一个 JSON 对象，单行上限 1 MiB。
- 客户端先调用 `aki.hello`；`aki.health` 可在握手前调用。
- 方法：`aki.chat.send/cancel/history`、`aki.approval.respond`、
  `aki.memory.status/tokens/tree/read/search`、`aki.fs.writable.*`、
  `aki.mqtt.*`、`aki.username.get/set`、`aki.conversation.reset`、`aki.status`、
  `aki.admin.zero_initiation_budget`（仅同 UID，且 `source_profile=aki`）。
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
控制主题 probe/presence/status、probe 去重与限流、多成员 roster 与 `@` 提及、
profile 级用户名、profile 解析与 systemd 模板、provider 隔离、人格模板渲染以及端到端流式输出。
