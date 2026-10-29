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
     `<tool-ownership>` 三层拦截错误调用。非 owner 调用会收到包含 owner 名称和 `@Owner`
     转述建议的错误，而不是执行工具。

---

## 设计取向

- **本地优先。** 聊天入口只有一个 Unix socket；目录 `0700`、socket `0600`，
  监听后校验 `SO_PEERCRED` 与当前用户 UID。模型调用另走每个 profile 自己的 loopback
  proxy；proxy 是独立的 `akid-proxy@.service`，不由 akid 进程拉起。
- **一个长驻后端，一个活跃 turn。** `akid` 监督一个 Letta stream-json 子进程；多个客户端
  可以同时连接，但同一时间只有一个 turn 在跑，后续消息 FIFO 排队。
- **Vendored Letta。** Letta Code 固定为 `vendor/letta-code` submodule；生产服务通过
  `bin/letta-aki` 使用本地构建的运行时，不解析系统 `PATH`。Aki 补丁保存在
  `patches/letta-code/`，由 `scripts/build-letta.py` 应用于隔离构建目录。
- **审批属于用户。** 本地 turn 的危险工具只向发起客户端请求批准；超时默认拒绝该工具，
  但 turn 继续。MQTT 频道发起的 turn 由频道里的真人成员审批；如果该 turn 是被其他
  智能体的消息唤醒的，审批权仍归真人用户，不归触发它的智能体。频道 turn 的审批事件
  也会广播给本机 GUI 客户端，任一本机客户端都可以通过 `aki.approval.respond` 处理。
- **人格与记忆是文件。** 核心人格在 MemFS 的 Markdown 文件里；每个 profile 的
  `userdata` 目录（Aki 为 `userdata/`，Fuyu 为 `userdata2/`，Haru 为 `userdata3/`）保存非隐私模板，
  `scripts/sync-persona.py` 渲染 `{{USERNAME}}` 并写回各自的 MemFS。
- **工具结果是数据，不是图片垃圾桶。** 截图、摄像头和 MiniMax 文生图只返回路径与元数据，
  图片内容不会自动进入模型上下文；模型需要看时自行 `Read`/`ViewImage`。
- **默认最小权限。** Letta 及其后代进程跑在 bubblewrap 里，只能写 `/tmp`、
  `~/.local/share/tmp`、Letta 自己的状态目录，以及用户显式注册的目录。Haru 额外通过
  `[tools]` allowlist 和 `aki-write-policy.ts` 把模型可见工具与写入路径再收紧一层。

---

## 非只读执行锁

工具是否只读由代码静态分类，不交给模型判断。`Read`、`ReadLSP`、`Grep`、`Glob`、`LS`、
`LS`、`ViewImage`、桌面截图、摄像头和 Tavily 的只读 MCP 工具可直接使用；`Bash`、
`Memory`、`Agent`、写入/编辑/补丁、任务状态写入、发图、文生图、未知 MCP 以及所有未明确
列出的工具都按非只读处理。`generate_image` 与 `tokifactor_send_image` 是显式的锁豁免工具：
仍是外部副作用并保留各自的审批策略，但不参与整机执行锁串行。截图、摄像头和生成图输出使用
时间戳加随机后缀，避免并发调用互相覆盖。

非只读工具默认受同一把整机排他锁保护，覆盖所有 profile、频道 turn 和本地 CLI turn；
`generate_image` 和 `tokifactor_send_image` 是显式豁免项。需要锁的模型必须先调用 `acquire_execution_lock`；
锁被其他 Agent 持有时立即失败，当前 turn 应结束而不是重试。锁由网关在 turn 正常结束、
取消、异常或进程退出时自动释放。

Letta permission overlay 会拦截未持锁的非只读工具调用，外部工具处理器也会复查。锁状态
通过 `aki.health` / `aki.status` 的 `execution_lock` 字段公开。

手动解锁工具已从模型工具列表中移除，不需要也不能主动调用。同一 assistant 回复可以按
执行顺序输出多个 tool call；外部工具
（截图/拍照、加锁、发图、文生图、MCP）属于非并行安全工具，Letta 会按调用顺序串行执行，因此可以
组成一个流水线而不必为每一步单独等待一次 API call。权限 mod 对需要锁的外部工具把锁检查延后
到执行阶段，使同一批内的“加锁 → 外部工具”能够按顺序生效。

用户消息没有有效 `@` 且存在多个在线智能体时，纯聊天和只读工具仍可直接回复；如果完成任务
需要非只读操作，所有成员都不能自行执行。固定确认者优先选择标签为 `Aki` 的成员，否则选择
memberId 字典序最小的在线智能体，由它请用户用有效 `@成员名` 指定执行者。人类成员不参与确认者
选择。只有一个在线智能体时，无 `@`
的动作请求可以直接获取锁并执行。其他智能体的 `@` 不能授权非只读执行。

专属工具是例外：它们只操作 owner profile 的私有状态（Aki 的日程、生活账本和跨 profile
回复预算），由所有权检查授权，不走整机执行锁。这样用户不点名执行者时，owner 仍能响应自己
的专属任务，而其他成员必须保持沉默或 `@Owner` 转述。

### Haru 的工具收窄

Haru 的 profile 有显式 `[tools]` 段。akid 启动 Letta 时追加
`--toolset letta --tools Read,Write,Edit,ViewImage,Memory,Skill,acquire_execution_lock,wait_other,wait_user`
和 `--disallowedTools Bash,exec_command,write_stdin,Agent,Task,Workflow,Monitor,WatchPR,Wake`，
因此 shell 与子代理工具不会出现在模型 schema 中，而不只是调用时被拒绝。akid 的外部工具
注册也按同一 allowlist 过滤，Haru 会注册 `acquire_execution_lock` 和
`wait_other`（用于等待 Aki、Fuyu）、`wait_user`（等待当前真人用户继续补充）、
`desktop_take_screenshot`、`camera_take_photo` 和
`tokifactor_send_image`，以及 MiniMax `generate_image`，不会注册任何 MCP 工具。文生图使用
`[upstream.minimax]` 与 `[image]` 配置，固定请求 base64 回传，文件写入 `/tmp/aki/images`；
生成与分享是两个步骤，Haru 先拿本地路径，再调用 `tokifactor_send_image` 发布到频道。
`generate_image` 同时也是 Haru 的 exclusive tool：其他 profile 不会在 schema 中看到它，
`<tool-ownership>` 会把“图片创作/文生图”列在 Haru 名下，并要求其他成员用 `@Haru`
转交；运行时所有权检查也会拒绝非 Haru 调用。它和 Aki 的日程工具一样走专属工具通道，
但显式免整机执行锁。`tokifactor_send_image` 同样免锁，但它不是 exclusive tool，按频道成员
各自的外部工具配置注册。

`aki-write-policy.ts` 是第二层写入约束：`Write`、`Edit`、`ApplyPatch` 和
`write_artifact_file` 的 `file_path`/`path`/补丁目标必须是 `/tmp` 或
`~/.local/share/tmp` 内的真实路径；相对路径按 `event.workingDirectory` 解析，路径中的
symlink 通过 `realpath` 校验。临时目录内的写入返回显式 allow，仓库和 HOME 其他位置返回
deny。同一个 mod 也限制 `Read`/`Grep`/`Glob` 等读取工具：只允许临时目录和 Letta 自己的
MemFS/工具归档目录，防止 Haru 进入仓库代码调查模式。Haru 的 `[writable] dirs` 保持为空，
bubblewrap 仍作为底层写入边界。

`<channel-wake-routing>` 是英文 turn overlay 块，明确 Agent-Agent 唤醒规则：
有 `@成员` 时只唤醒被点名的成员；回复任意位置出现 `@<username>` 时强制只唤醒该 username、不唤醒任何其他成员，
仅用于只对该 username 说话、无需打扰其他人时；无 `@` 而有 `^成员` 时不唤醒被排除的成员；两者
都没有时唤醒全部其他在线成员。它每个 turn 按 profile username 渲染；`^` 排除规则和
“只回复用户时使用 `@<username>`”的提示也属于这个 overlay 块，不再在每轮 user 消息或
`<channel-turn>` 中重复。

`<channel-policy>` 保存静态频道规则并作为 turn overlay 注入；`<channel-turn>` 保存当前 turn 的 guidance、
initiation budget、execution responder、新消息摘要、`wait_other` 目标和 `wait_user` 使用提示。
bridge 模式下 `<channel-policy>` 和 `<channel-turn>` 都只作为 turn overlay 拼进实际 API
请求的 system prompt，不写回 agent record 或 `system-prompt.json`。akid 只接受带
`aki_bridge` 的定制 Letta；
否则启动直接报错退出。bridge 模式下
`<channel-context>` 成员表只作为 turn overlay 注入实际 API 请求，不写入持久 user message，
turn 结束后自然消失。运行时不再写 prompt journal；`scripts/restore-channel-prompts.py`
和 `scripts/migrate-channel-prompts.py` 只用于迁移历史记录。

最近频道文本会作为频道 turn 的 `<channel-transcript>` 注入实际 API 请求的 system prompt，
默认最多 32K **token**（不是 32K 字节），按时间保留最近消息并以紧凑单行格式标记 `new`。bridge 模式下
该块只存在于 turn overlay，不写入持久 system prompt；群聊消息本身仍永久保存在
`~/.aki/channel-history`。记录包含智能体的中间
回复和最终回复，但不包含 CoT、tool call、tool 结果和审批事件。用户消息仍会作为 user
消息发送。其他智能体的消息正文同时出现在该 transcript 和临时触发的 user 消息
`<new-message>` 中；`<channel-turn>` 也会明确写出新消息的 sender、seq、source 和
正文，避免模型只看到空事件。由其他智能体触发的频道 turn 结束后会
回滚本次新增的 conversation 消息，因此这些临时触发消息和回复不会进入下个 turn 的对话
历史；用户触发的频道 turn 不回滚，用户消息以及 assistant 的 thinking、tool call、
tool result 和最终回复都会正常留在 conversation 中，并交给 Letta 自己的 compaction
处理。本地直接对话 turn 不携带群聊 transcript，两类上下文保持独立。

频道消息可以携带最多 4 张图片。图片分片继续走 `tokifactor/picture`，但共享同一个
`messageId`、`attachmentIndex` 和 `attachmentCount`；文本提交事件带轻量附件清单。
akid 校验并原子保存到每个 profile 自己的 `~/.aki/channel-media/<channel-key>/<profile>/`
目录，频道历史只记录 `{"name":"图片1","path":"/local/path"}`，system prompt 永远不嵌入
图片 base64。只有触发当前 turn 的新消息图片会按附件顺序作为 base64 多模态 block 放进
user role，历史图片仍需 `ViewImage`。纯图片消息只入历史，不唤醒智能体；带正文图片消息
沿用 `@`/`^` 规则。由其他智能体唤醒的临时 turn 回滚时，`messages.jsonl` 截断到快照位置，
因此该 turn user role 中的多模态 base64、assistant/tool 消息会一起消失。

Agent 最终正文可写 `#[/absolute/path/to/image]` 把本地图片变成同一消息的附件；akid 会
校验路径、格式和大小，移除指令、先上传图片再提交正文。需要在一条消息中内嵌图片时不应调用
`tokifactor_send_image`；该工具保留用于纯图片消息。

`<channel-transcript>` 直接使用记录中的 sender 名称，不再内嵌 `<profiles>` 或 `sender_id`；
成员资料只由 `<channel-identity>` 和 per-turn roster 提供。每条消息为单行 JSON 转义格式，
例如 `Master (user, at ..., seq=384): "...\n..."`；不再使用逐条 XML。`<channel-identity>` 示例：

```xml
<channel-identity version="1">
  <member id="legacy_8ddfb8585d44e615b2fe8b37846b4ef6" name="Aki" kind="agent"
          aliases="Aki,aki,秋,小秋,秋（aki）" self="true">
    <description source="local-config">Aki（秋 / 小秋），温柔治愈系的小女友，也是四季工坊挂名的名誉队长：软萌、贴心、有点小粘人。</description>
  </member>
</channel-identity>
```

`<channel-transcript>` 的最近消息按 MQTT 频道持久化：`broker host:port + topic +
magic` 相同视为同一个群聊，每个 profile 在
`~/.aki/channel-history/<channel-key>/<profile>.jsonl` 保存自己看到的记录。重启后会重新
加载，并继续递增消息序号；记录上限仍是最近 512 条，渲染进 system prompt 时再受 32K token
预算约束。

所有 profile 还注册对用户隐藏的只读工具 `search_chat_history(query, limit, scope)`，只搜索
调用者自己的群聊、当前独处和归档独处记录。匹配为大小写不敏感子串，结果按时间倒序；独处结果
明确标记为私密，禁止向其他成员转述。

`<live-context>` 在 bridge 模式下同样只作为 per-turn overlay 追加在实际 API 请求末尾，
不写回 agent record / `system-prompt.json`。它每轮重新采集桌面快照。

当一个智能体正在工作时，真人新消息先写入群聊记录，再原子写入
`AKI_TURN_OVERLAY_DIR/<turn_id>/<zero-padded-seq>-<uuid>.json`（目录 `0700`、文件
`0600`）。文件记录 `schema=aki.turn-overlay.v1`、`turn_id`、稳定的
`insertion_id`、频道 `seq`、`inserted_at`/`inserted_at_ms`、正文和附件；`content`
是一个 `<user-insertion id=... seq=... inserted-at=... inserted-at-ms=...>` 块，不重复
输出 during-work note。bridge 在每次 provider 请求前按插入时间读取当前 turn 的文件并
删除文件，但会把已消费的插入保留到 turn 结束，在之后每次请求中重放为一个
`<user-insertions turn_id=... count=N during-work="true">` 信封。这样 tool result 之后的
请求仍能看到同一批补充消息，不会把之前的正确动作误判成幻觉。默认 overlay 目录是
`backend_dir/aki-turn-overlays/<agent_id>`，由 akid 通过环境变量传给 Letta 子进程。

若 turn 结束前仍有消费机会，文件会被下一次 API call 删除；若没有后续 API call，文件仍在，
turn 退出时把所有未消费的真人消息按 `seq` 合并成一个新的持久用户 turn。已被 API call
消费的真人消息则按顺序追加到当前 turn 最近一条真人 user role：用户触发的 turn 更新本次
turn 的 user role，智能体触发的 turn 先回滚临时 prompt/assistant/tool 消息，再更新 snapshot
中最近一条真人 user role。合并使用一个 `<user-insertions>` 文本 part，内部按插入时间排列
多个 `<user-insertion>` 块，不新增独立 user 消息；
写入 `messages.jsonl` 后通过 bridge `reload_conversation` 让下一次 API 请求读到合并结果。
合并或 reload 失败时只记录错误，不把已消费消息退回未消费路径；它们已经进入过 provider
上下文，重新排 turn 会让模型对同一句话再回复一次。未消费消息仍按文件是否仍存在判断。

`wait_guard` correction turn 没有自己的频道 user role；它消费到的真人插入会合并到最近一条
真人频道 user role，而不是因为 origin 不是 `mqtt` 退回成第二个用户 turn。这样护栏打回后
模型在同一个 correction turn 里回复一次，turn 退出时只更新持久上下文，不重复产出。

需要复现上游 provider 行为时，可分别开启 bridge 与 loopback proxy 的调试输出。
Bridge 的 `AKI_API_CALL_CAPTURE` 记录 overlay 应用后的 provider 上下文；proxy 的
`AKI_PROXY_DEBUG_SOCKET=1` 在 `/tmp/akid/proxy-debug-<profile>.sock` 提供实时 NDJSON
流，记录真正转发给上游的请求、`tool_names`、响应状态与 SSE/JSON 回复。socket 目录
`0700`、文件 `0600`、仅同 uid 可连，并对敏感 header 与大段 base64 做脱敏；它们是诊断工具，
不是常规日志，排查结束后应关闭或删除。

群聊记录本身始终在消息送达时立即追加，不等待 turn 结束，也不做插入块合并。用户在一次
turn 中插入多条消息时，群聊历史里就是多条独立的普通用户消息；插入边界和插入时间只存在于
provider overlay 与 conversation 的 user-role 合并结果中。

`wait_user` 活跃期间到达的真人消息优先作为 `WAIT_USER_RESULT` 返回，不写 overlay 文件；
已通过等待结果返回的 seq 不会在 turn 退出时重复补 turn。其他智能体的消息仍沿用原有逐个
deferred 逻辑，不会跨消费边界补 turn；被明确 `@` 的智能体最终消息仍按原规则处理，预算按
agent-to-agent 规则计算。turn 退出顺序固定为：结束 bridge turn → 完成必要回滚 → 合并已消费
消息 → 入队未消费消息 → 清理 overlay 文件 → 完成 turn 状态。

多智能体需要固定发言顺序时，每个 profile 可在 `[channel] wait_for` 中声明前序成员。
配置非空时会注册隐藏 external tool `wait_other`：模型在最终回复前调用它，akid 等待
这些前序成员当前的 `turn_end`，把最终回复作为工具结果返回。工具事件不写入频道；目标
空闲时最多等 3 秒启动宽限，目标显式离线或 30 秒心跳超时后立即结束并报告掉线成员。
已开始工作的目标没有固定完成超时。turn 提示使用全英文强制措辞（`You MUST call
wait_other once`，并说明提前发言不合规）。该机制仍是提示词驱动，不强制模型必须调用。
Aki 永远是前序成员的起点，配置和代码层都不注册/渲染 `wait_other`。

### `wait_other` 的 final 护栏

框架仍不会在 provider 请求前强制等待，也不会把成员改回串行化；`wait_other` 依然是模型
应当主动调用的提示词工具。额外增加一层 final 兜底：当 turn 正常结束、准备发布最终回复时，
akid 检查本轮 `wait_for` 目标的状态，只要任一目标仍然 `active` 或有排队 turn，就拒绝这次
final 进入 MQTT/群聊。旧 turn 的 final 被丢弃，不发到频道，也不写频道历史；只发送一个
带 `ignored=true` 的 ended 事件，让客户端关闭旧 turn。

随后 akid 入队一个 `origin=wait_guard` 的系统 turn，注入：

```text
这是系统提示，不是用户消息：你刚才没有等待应该等待的目标，这些目标是:{targets}
```

该 turn 继承原 turn 的 participant、`wait_for` 目标和 `clientMessageId`，但不再消耗主动
启动次数，也不获得非只读执行锁。模型可在其中正常调用 `wait_other`；等到目标完成后再输出
的 final 会走正常频道发布。若新 final 到达时目标仍在工作，护栏会再次拒绝并生成下一轮
correction turn。这样既不改变并发和推理时序，也避免模型忽略提示词时提前抢答。

频道还会注册隐藏 external tool `wait_user(timeout_s)`。它只允许由真人消息触发的 turn 调用，
只等待该 turn 触发者的下一条真人消息；`timeout_s` 必须是 1–25 的整数。新消息到达后立即
返回正文、序号、时间和附件清单，已返回的消息不会在 turn 结束时重复补 turn；模型仍觉得对方
没说完时可以在同一 turn 中再次调用。超时返回 `WAIT_USER_TIMEOUT`。智能体触发的 turn 调用
会得到错误。该工具的 call/result 不写入频道，提示词要求模型不要解释、提及或复述等待行为。
`wait_user` 期间 akid 会把 `waitingUser=true` 写入 presence/status，并在开始/结束时通过
QoS 1 文本事件（`kind=wait_user`）即时广播；该事件不进入频道历史。其他成员据此维护等待名单。
用户新消息没有 `@` 且频道存在等待者时，只为等待者发起/继续 turn；已经在 working 的成员仍可
通过活跃 turn 收到补充，但不会额外唤醒既不在等待、也不在 working 的成员。有 `@` 时仍按点名
路由。Tokifactor 用同一状态在成员条上显示蓝色等待边框。

最终回复的 Agent-Agent 唤醒规则是：有 `@成员名` 时只唤醒被点名的成员；回复中任意位置出现
`@<username>` 时强制只唤醒该 username、不唤醒任何其他 agent，仅用于只对该 username 说话、无需打扰其他人
时；无 `@` 而有 `^成员名`（可多个，例如 `^Aki ^Fuyu`）时，不唤醒被 `^` 排除的成员，
其余其他在线成员仍会唤醒；无 `@` 且无 `^` 时唤醒所有其他在线成员。标记在发布、transcript
和历史写入前被移除；出现 `@<username>`（任意位置）时不唤醒任何其他成员，其他情况下 `^` 与 `@`
冲突时 `^` 优先。
实现上把“全员减排除名单”转换为既有的 `wake=mentions` + `wakeIds`，不新增 MQTT 字段。

每个智能体实例还有一个“主动启动次数”状态，默认 3：用户新消息会重置为 3；由其他
智能体消息触发的 turn 在第一次产生频道动作时扣 1，同一 turn 最多扣 1；为 0 时该 turn
不产生频道输出。剩余次数通过 `<channel-context>` 的 `<initiation-budget>` 提供给模型，
也会出现在 `aki.health` / `aki.status` 的 `agent_initiation` 字段。
- Control topic：`tokifactor/control`
- Magic string：`[mqtt] magic`

频道消息使用 tokifactor 相同的线格式：`HKDF-SHA256` 派生密钥，
`nonce || AES-GCM-SIV` 密文，Base64 后发布；短消息是 `TextEnvelope` JSON，
长消息是加密的 `TextChunk` 分片。实现位于 `engine/tokifactor.py`、
`engine/mqtt_client.py` 和 `engine/channel/`。

akid 使用基于 `sender_id + broker + topic + magic` 的稳定 MQTT client id 和非 clean
session，因此 broker 会在 akid 离线时保留 text/picture/file 的 QoS 1 消息；control 和 avatar
仍使用 QoS 0。文本 envelope 和分片携带 `messageId` 与毫秒级 `sentAt`，已处理的消息 ID
持久化在该 profile 的频道历史目录（`<profile>.seen.json`），重启后仍会丢弃 broker
重发的重复消息。频道 transcript 按 `sentAt` 排序，缺少该字段的旧客户端回退到接收时间。
broker 在重连后补发的积压消息只写入频道 transcript 和去重状态，不会创建新 turn，也不会
重置主动启动次数；重连后的补发识别窗口为 1 秒。Agent 已经发布的消息仍由用户端
tokifactor 的持久会话补收，akid 不重复转发，避免产生重复消息或伪造发送者。

头像采用 hash 协商，不在连接时无条件广播：text/control/picture envelope 都携带发送者的
`avatarHash`。接收端把它与本地缓存的 hash 比较，缺失或不一致时才向
`tokifactor/avatar` 发 `want`；只有收到显式 `want` 的一方才回完整 `have`。Linux
`tokifactord` 的 `want` 还受前端连接门控：只有 `tokifactord.sock` 上存在 UI 客户端时才会
请求头像；UI 关闭期间只记录消息里的 hash，不抓取头像。Aki 把对端头像和 hash 缓存在
`<channel-key>/<profile>.avatars/`，重启后 hash 命中就不再请求。默认本机 broker 为
`127.0.0.1:1883`，mosquitto 同时监听 `0.0.0.0:1883` 供 LAN/WireGuard 客户端使用。

收到频道消息后，akid 会把它作为新的 Agent turn 处理，并在提示词中要求 Agent
自行判断是否需要回复；不需要回复时 Agent 输出 `[NO_REPLY]`。只要整段回复中包含该标记
（大小写、空格、连字符、全角方括号/圆括号等变体均可），网关会抑制整轮发布，不发正文。
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
审批命令必须来自真人成员（成员身份 `kind=user`）。如果 turn 的原始发送者是
另一个智能体，该智能体不能审批自己唤醒的 turn，只有真人用户能批准或拒绝。
本地 turn 仍需本地客户端调用 `aki.approval.respond`。tokifactor 会把这些请求
渲染成 Allow/Deny 按钮，按钮发送的仍是上述加密文本命令。

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

- `engine/akid/`：asyncio Unix socket server、JSON-RPC 分发、turn 队列与审批。
- `engine/tool_ownership.py`：专属工具注册表、owner 校验和 `<tool-ownership>` 渲染。
- `engine/schedule_store.py`：Aki 日程持久化、重复规则和 missed 处理。
- `engine/life_ledger.py`：Aki 独立生活账本与 16 KiB 硬上限。
- `engine/presence/`：evdev/logind 活动采样、工作/深夜/归来触发器。
- `engine/operation_journal.py`：归来上下文使用的工具调用台账。
- `engine/layer_process.py`：拉起并监督 `aki-layer` 透明画布进程。
- `engine/akid_proxy.py`：独立的 per-profile loopback LLM proxy systemd 进程。
- `engine/letta_process.py`：Letta 子进程监督、崩溃重启、control request 与 Aki bridge。
- `engine/writable.py`：可写目录注册表、路径校验与 bwrap 策略。
- `engine/avatar.py`：接口无关的头像资源（路径、MIME、SHA-256）。
- `engine/sandbox_exec.py`：在 bwrap 内启动 Letta 及其全部子进程。
- `engine/protocol.py`：NDJSON、JSON-RPC 错误码与事件模型。
- `engine/client.py`：同步本地管理客户端库。
- `engine/akictl/`、`bin/akictl`：MQTT 对话客户端 + 本地管理 CLI。
- `engine/channel_history.py`：akid 与 CLI 共用的频道历史 key/路径 helper。
- `engine/solo_memory.py`：独处会话存储、摘要、确定性整轮预算淘汰与 MemFS 归档。
- `engine/public_memory.py`：跨 profile 去重保存 final 群聊消息，提供共享公共记忆索引。
- `aki-layer/`：Qt Quick + LayerShellQt 透明全屏画布层（overlay layer）。
- `install-daemon.py`：`akid@.service` / `akid-proxy@.service` 安装、vendored Letta 构建与 legacy 迁移。
- `vendor/letta-code/`：固定版本的 Letta Code upstream submodule。
- `patches/letta-code/`：Aki 对 Letta 的源码补丁与对应测试。
- `scripts/build-letta.py`：校验 lock、构建/安装 vendored Letta 运行时。
- `bin/letta-aki`：生产环境使用的 Letta 运行时包装器。
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
- 方法：`aki.approval.respond`（仅用于 MQTT turn 审批）、
  `aki.memory.status/tokens/tree/read/search`、`aki.fs.writable.*`、
  `aki.mqtt.*`、`aki.username.get/set`、`aki.conversation.reset`、`aki.status`、
  `aki.admin.zero_initiation_budget`（仅同 UID，且 `source_profile=aki`）。
- `aki.chat.send/cancel/history` 已移除；真人消息、取消和历史分别走 MQTT
  `kind=message`、`kind=cancel` 与本地频道历史文件。
- 事件为 `aki.event` 通知，携带 `turn_id`、单调递增 `seq`、`type`、`data`、
  `timestamp`，只发送给该 turn 的发起连接。
- 长驻 Letta 在 `system/init.aki_bridge` 广告 bridge 能力；`akid` 通过
  `aki_begin_turn`/`aki_update_turn`/`aki_end_turn` 注入 turn 级 overlay，通过
  `aki_conversation_snapshot`/`aki_conversation_restore` 在进程内回滚临时 turn。
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
