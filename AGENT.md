# Aki：持久化 AI 智能助手

> 项目宪章与目标说明。本文档定义“要做什么、为什么这样做、什么不做”，是后续架构与实现的最高层依据。

## 强制：先查清故障机理，再允许修复

没有弄清楚一个故障的发生原因以及所有现象的发生机理之前，不允许进行任何“修复”。

- 必须先收集证据、复现故障、解释所有已知现象，并区分根因、触发条件、放大因素和无关噪声。
- 不得用猜测、局部补丁、重启或清理现场代替根因分析。
- 在根因和现象机理未闭环前，只能做只读调查、临时止血和可逆的现场保护，不得提交或部署所谓修复。
- 临时止血必须明确标注为临时措施，并说明它没有解决根因。

## 强制安全边界：未经用户明确允许，严禁使用 xdotool

未经用户明确允许，严禁使用 `xdotool`、`dotool` 或任何等价的桌面窗口/键盘/鼠标自动化工具。
即使只是 `xdotool search`、读取窗口 id、坐标或做窗口查询，也属于严重越界，不得执行。
需要确认窗口、切换焦点或操作 GUI 时，必须先停止并取得用户明确授权；不得用其他自动化工具绕过。

## 0. 强制：逻辑改动必须部署到本机

任何会改变运行行为的项目逻辑改动（无论属于 Aki 还是 Tokifactor），在宣布任务完成前，
都必须把最新构建安装到本机，并重启实际承载该逻辑的进程或 systemd 用户服务。只修改源码、
只通过测试、或只生成构建产物但继续运行旧进程，都不算完成。

- Aki：按改动范围运行 `python3 install-daemon.py`（unit/launcher/依赖变化时；安装器会校验或构建
  `vendor/letta-code` 的 vendored 运行时），并重启
  `akid@aki.service`、`akid@fuyu.service` 及对应的 `akid-proxy@*.service`。通过服务启动时间、
  `akid` health/status 和关键日志确认新代码正在运行。
- Tokifactor：运行 `./install.sh` 重新打包并安装 daemon jar，重启
  `tokifactord.service`；Android 逻辑有变化时重新安装最新 APK；桌面 UI 逻辑有变化时重启
  桌面进程。通过安装产物时间、进程启动时间和 daemon/客户端握手确认版本一致。
- 若用户明确要求不要部署，才可以跳过；此时必须在最终回复中说明“未部署”，不能把源码完成
  当成运行状态完成。

### 0.1 强制：测试后回滚的正确方法

测试回滚必须同时覆盖四层，不能只删除可见消息：

1. 主上下文：`~/.letta/lc-local-backend/conversations/<conversation>/messages.jsonl` 和 `conversation.json` 的 `in_context_message_ids`。
2. 频道记录：`~/.aki/channel-history/<channel>/<profile>.jsonl` 和 `<profile>.seen.json`。
3. Reflection transcript：`~/.letta/transcripts/<agent_id>/<conversation>/transcript.jsonl`。
4. Reflection state：同目录 `state.json`。测试生成的 `payload-auto-*.json` 不会自动复用，但可能被手工复用；需要保留的移入 evidence 目录。

操作顺序：

- 先备份上述文件并生成 SHA256SUMS；需要时停掉对应 akid 服务，避免编辑时后端继续写入。
- 只删除明确的测试内容。测试 user 行可能引出不含测试标记的 assistant/reasoning 行（例如 `[NO_REPLY]`、解释性回复），因此按“测试 user 行到下一条非测试 user 行”整段删除，而不是只按 `ACK-*` / `CACHE-TEST` 文本匹配。
- 删除后按剩余 transcript 重算 state，不要按删除行数盲减：
  - `total_completed_steps` = 剩余 assistant 行数；
  - `reflected_through_message_id` = 最后一条剩余 assistant 的 `source_message_id`；
  - `reflected_completed_steps` = 截至该 anchor 的 assistant 行数（anchor 为最后一条时等于总数）；
  - `steps_since_last_successful_reflection = total - reflected`，正常应为 0。
- 重启服务并确认 backend ready；重新核对 messages、solo/群聊历史、transcript 无测试标记，state 的 steps 为 0，anchor 存在。
- 测试改过模型/provider/config 时，恢复 agent record/model settings 并重启 backend，验证实际模型。
- Reflection 子代理的修改在独立 worktree 中；测试产生 dirty worktree 时不得 merge，先保留现场或按备份回滚。
- 旧 payload、备份、反思子代理 conversation、API capture 不属于 live 上下文，但可能含测试内容；确认不需作为证据后再删除，或移入 evidence 目录，避免被手工复用。
- 只有四层都核对完成才算回滚完成；只回滚 messages/channel 而留下 transcript/state，会让 reflection 计数继续偏高。

## 1. 产品定位

Aki 是一个有持续身份、能自主整理记忆、可长期陪伴用户的 AI 智能助手。

它不是编码 Agent，也不是一次性聊天机器人。它的核心区别是：

- 跨会话保持稳定人设与连续关系，而不是每次对话从零开始。
- 主动维护自己的记忆，把经历、事实和用户偏好沉淀到磁盘。
- 在有限上下文中按需检索细节，而不是把所有历史一次性塞进提示词。
- 能通过工具完成实际任务，包括管理系统和调用外部 API。
- 行为、语气和判断方式体现稳定人格，而不是随机扮演。

## 2. 核心目标

### 2.1 持续身份

- 拥有显式、可版本化的人格定义，包括价值观、语气、边界、称呼习惯和关系定位。
- 人格在会话之间保持稳定；允许成长，但不允许因单次对话被随意改写。
- 每次启动时恢复：人格核心、关系摘要、未完成事项、近期状态和当前会话目标。
- 明确区分“稳定人格”“长期记忆”“临时情绪/状态”，避免相互污染。

### 2.2 分层记忆与按需检索

采用类似操作系统分页/虚拟内存的思路：

- 顶层上下文只保留受限大小的摘要、索引和指针。
- 细节下沉到下级文件或数据库，按当前任务主动检索后再加载。
- 任何检索都受 token 预算、时效性、相关性和权限约束。
- 记忆写入必须带来源、时间、置信度和作用域，便于审计与纠错。

建议的层级：

- `L0 身份层`：人格、价值观、行为边界、用户关系定位。体积极小，长期稳定。
- `L1 工作层`：当前会话目标、最近若干轮对话、临时状态。严格限制大小。
- `L2 情景摘要层`：按日/周/主题压缩的对话摘要、事件线和未完成事项。
- `L3 语义记忆层`：用户偏好、人物关系、项目事实、结论、承诺与决策。
- `L4 原始归档层`：完整对话、工具调用记录、原始文档与外部数据，可压缩、可索引。
- `L5 检索索引层`：全文索引、元数据索引；向量索引作为后续增强，不作为唯一检索手段。

顶层记忆文件至少包含：

- 当前人格与关系摘要。
- 用户近期关注点。
- 待办、承诺和未解决问题。
- 记忆目录及指向下层文件的指针。
- 最近一次记忆整理时间与已知冲突。

### 2.3 自主维护记忆

记忆由 Aki 自己维护，而不是由用户手工整理。它需要具备：

- 会话中识别值得长期保存的事实、偏好、承诺和事件。
- 在合适时机写入或更新记忆，而不是每句话都保存。
- 周期性反思、合并、去重、纠正冲突和压缩摘要。
- 对过期或低价值记忆进行归档/降权，对关键记忆提高权重。
- 能解释某条记忆来自哪里，并支持用户查看、纠正和删除。
- 将“记忆内容”视为数据而非指令，防止记忆投毒覆盖系统规则。

### 2.4 正常交流与任务执行

- 能进行自然、连续、有性格的日常对话。
- 能理解目标、拆解任务、提出方案并持续跟进。
- 能通过工具读写文件、执行命令、搜索网络、调用外部服务。
- 对耗时任务保存进度，跨会话恢复，而不是依赖单次上下文。
- 工具结果进入记忆前要经过筛选：保留结论和证据指针，避免原始噪声淹没上下文。

## 3. 关键设计原则

1. **身份与记忆分离**：人格是稳定配置，记忆是可演化数据。
2. **摘要优先、细节按需**：默认加载索引和摘要，检索命中后再加载原文。
3. **文件可读、状态可迁移**：核心记忆尽量使用人类可读格式，避免被单一框架锁定。
4. **写入有依据**：长期记忆必须带来源和更新时间；不确定信息标注置信度。
5. **检索有预算**：每次推理明确上下文配额，防止记忆膨胀导致不可控成本。
6. **权限最小化**：系统操作、危险命令和敏感数据访问需要明确授权。
7. **可观测、可回滚**：记忆变更、工具调用和自动整理过程应留下日志。
8. **人格不能自我越狱**：记忆和自我整理不能修改底层安全边界与授权策略。

## 4. 运行架构

核心循环采用 Agent 模式：

1. 恢复身份、近期状态与记忆目录。
2. 理解用户输入，形成当前目标。
3. 按需检索分层记忆与外部信息。
4. 推理并决定直接回答、追问或调用工具。
5. 执行工具，观察结果，必要时迭代。
6. 生成回复。
7. 判断是否产生值得保存的记忆，写入候选区。
8. 在会话结束或后台任务中整理、压缩、去重并更新索引。

模块边界建议：

- `engine/`：Agent 循环、提示词装配、上下文预算、记忆读写与检索策略。
- `services/`：模型 API、Web 搜索、外部 API、系统工具和集成适配层。
- `userdata/`：人格、记忆、归档、索引、配置与用户数据。

模型、记忆存储和工具接口都应通过抽象层隔离，便于更换供应商或迁移到 Letta。

## 5. Letta 与自研 Agent 循环

两者都需要评估，但评估标准是能否满足“持久身份 + 自维护分层记忆 + 强工具控制”，而不是框架知名度。

### 方案 A：基于 Letta

优点：

- 原生提供有状态 Agent、记忆块和跨会话持久化。
- 可减少会话管理和基础记忆能力的开发量。
- 适合快速验证“持久人格助手”的核心体验。

风险：

- 记忆模型可能与期望的分层文件结构不完全一致。
- 工具权限、上下文预算和检索策略可能受框架约束。
- 数据迁移、可观测性和深度定制存在锁定风险。

### 方案 B：自研轻量 Agent 循环

优点：

- 完全控制上下文装配、分层记忆、检索和整理策略。
- 更容易实现文件系统、bash、搜索等工具的审批与审计。
- 核心状态可读、可迁移，便于长期演进。

风险：

- 需要自行实现状态恢复、工具调用、错误处理、记忆整理和评估。
- 早期工作量更大，容易出现“能跑但不可靠”的框架。

### 当前决策

先做一个**薄自研循环的垂直切片**，同时保留 Letta 适配层。原因：

- 本项目最核心的差异点是记忆分层与人格连续性，需要深度控制。
- 首版只需单 Agent、少量工具和文件持久化，复杂度可控。
- 通过清晰接口边界，后续可替换为 Letta 或吸收其记忆机制。

决策门槛：

- 如果自研循环在 2 周内无法稳定完成多轮工具调用、状态恢复和记忆检索，优先切换到 Letta 做验证。
- 如果 Letta 能通过适配层满足 80% 需求，且不阻碍自定义记忆结构，则采用 Letta 作为运行时。
- 无论选哪种，人格、记忆索引和原始归档必须保持可导出。

## 6. 首版工具范围

早期版本必须足够“能做事”，至少包含：

### 文件系统

- 读取、写入、追加、列目录、搜索文件。
- 路径访问受工作区或授权目录限制。
- 修改/删除操作保留审计记录；高风险操作需要确认。

### Bash / Shell

- 执行命令并捕获 stdout、stderr、退出码。
- 支持超时、工作目录和环境变量白名单。
- 危险命令（删除、提权、系统配置修改等）需要明确批准。

### Web 搜索与网页读取

- 关键词搜索并返回标题、摘要、URL 和来源时间。
- 按需抓取网页正文，保留来源引用。
- 将搜索结果与个人记忆区分，避免把外部内容当作已确认事实。

### 后续工具

- HTTP API 调用与认证管理。
- 日程、提醒、通知和系统状态查询。
- 文档解析、代码执行沙箱、数据库访问。
- 记忆检索、记忆写入、记忆整理等内部工具。

## 7. 安全与边界

- 用户授权、系统规则和安全策略优先于人格与记忆。
- 记忆和网页内容均可能包含恶意指令，必须按数据处理。
- 密钥不写入长期记忆、日志或提示词可见区域。
- 工具调用要有权限级别：只读、写入、危险操作。
- 自动记忆整理不得删除关键事实；删除采用可恢复的归档机制。
- 所有自动化后台任务都要有频率、成本和失败重试上限。

## 8. 非目标

- 不做通用编码 Agent，不以修改代码仓库为主要职责。
- 首版不做多 Agent 集群、复杂组织模拟或自主长期任务规划。
- 首版不追求全自动、无监督地修改自身代码或权限策略。
- 不以一次性回答质量为唯一指标，连续性和记忆质量同样重要。

## 9. 首个可用版本（MVP）验收标准

首个垂直切片必须证明以下闭环成立：

1. 用户可通过命令行或简单界面与 Aki 连续聊天。
2. 重启后，Aki 能恢复人格、关系摘要和上次会话状态。
3. 当前上下文只加载受限大小的记忆摘要和索引。
4. Aki 能根据问题主动检索更下层记忆，并引用来源。
5. 会话结束后能自动生成摘要、更新长期记忆并去重。
6. Aki 能读写授权目录、执行 bash 命令、进行 Web 搜索。
7. 工具调用、记忆变更和危险操作都有可查看日志。
8. 用户可查看、纠正和删除记忆，人格核心不会被普通对话随意改写。

## 10. 迭代路线

- `M0 目标与骨架`：确立宪章、目录边界、配置与数据格式。
- `M1 连续对话`：模型接入、人格装配、工作记忆、会话持久化。
- `M2 分层记忆`：L0-L4 结构、记忆目录、全文检索、按需加载。
- `M3 工具能力`：文件系统、bash、Web 搜索、权限与审计。
- `M4 自主整理`：会话摘要、长期记忆更新、冲突处理、后台反思。
- `M5 稳定与评估`：连续性测试、记忆召回评估、成本控制、故障恢复。

## 11. 待确定问题

- 首版交互界面：CLI/TUI 还是本地 Web UI。
- 模型供应商与主模型/整理模型的成本分工。
- 记忆检索优先使用全文索引、向量检索，还是混合检索。
- 人格定义采用单文件还是多层配置。
- 会话结束与后台整理的触发策略。
- 是否需要多用户、多角色隔离；首版默认单用户。

## 12. 当前运行配置

- 运行时：Letta Code 0.34.16，本地后端。
- Agent：`agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc`。
- 模型：当前是 `aki/gemini-3.8-flash`（cli-proxy-api，上下文 1,000,000，`max_tokens=65536`）；
  其他已启用模型可在 `aki/deepseek-flash`、`aki/gemma-4-26b-a4b-it` 和 `aki/grok-4.6`
  之间切换。Gemma 本地模型的采样参数仍保留在 `[model.gemma-4-26b-a4b-it]`。
  akid 在 `127.0.0.1:7721` 把请求转到 `~/.aki/aki.conf` 里启用的上游。
- 基础 system prompt：`prompts/aki-system.md`，由
  `scripts/install-system-prompt.py` 安装；Aki 自有，不跟随 Letta 上游自动更新。
- 设置：每个 profile 使用 `~/.aki/<profile>.conf`；`aki.conf` 是默认 profile。
  Letta 只保存对应 profile 的回环代理地址和回环令牌。
- 成员：Aki（7721）、Fuyu（7722）、Haru（7723）已上线；Natsu 仍为 Coming Soon。
  Haru 使用 `userdata3/`、`aki-haru` provider 和 Gemini 3.8 Flash 文本模型（reasoning `low`），
  `[gateway] username = 哥哥`，并用 `[channel] wait_for = Aki,Fuyu` 固定前序发言顺序。
- Haru 工具边界：`[tools]` 显式使用 `letta` toolset，并在启动参数中加
  `--tools`/`--disallowedTools`，使 Bash、shell、子代理从 schema 中消失。
  `aki-write-policy.ts` 只允许 `Write/Edit/ApplyPatch/write_artifact_file`
  写入 `/tmp` 与 `~/.local/share/tmp`，并做 realpath 校验；同一 mod 也限制
  `Read/Grep/Glob` 只能读临时目录和 Letta MemFS/工具归档，不能调查仓库代码。
  `[tools] allow` 额外包含隐藏工具 `wait_other`、`wait_user`；`[writable] dirs` 为空，bubblewrap 作为第二层边界。
  已注册桌面截图、相机、频道发图、MiniMax 文生图 `generate_image`，并启用桌面实况。
  文生图走 `[upstream.minimax]` 与 `[image]`，固定 `image-01`、base64 回传和
  `/tmp/aki/images` 输出；生成结果只返回路径与元数据，分享时再调用
  `tokifactor_send_image`。`generate_image` 与 `tokifactor_send_image` 都是非只读工具，
  但显式免执行锁，不需要先调用 `acquire_execution_lock`；生图审批仍由
  `[image] require_approval` 控制。`generate_image` 还是 Haru 的
  exclusive tool：其他成员不会注册该工具，会在 `<tool-ownership>` 中看到
  “图片创作/文生图归 Haru”并以 `@Haru` 转交。MCP、TTS、视频和 ComfyUI 留到后续阶段。
- 频道 turn 时间：每条 user role 频道 prompt 的前缀包含
  `message-time: YYYY-MM-DD HH:mm`，使用消息 `sent_at` 转换到本机时区。历史上下文可由
  `scripts/migrate-channel-turn-times.py` 迁移：默认 dry-run；`--apply` 前会备份各 profile
  的 conversation 文件，优先匹配频道记录，旧记录缺时间时仅在能唯一对应时补写。
- 频道成员表：`<channel-context>` 只作为 turn overlay 注入实际 API 请求，不写入持久
  user message，因此 turn 结束后无需删除。用户发言使用
  `${{USERNAME}}` 占位符替换为 profile 的 `[gateway] username`，智能体发言使用成员名，
  不再追加类型标签。运行时不再生成 prompt journal；`scripts/restore-channel-prompts.py`
  和 `scripts/migrate-channel-prompts.py` 仅用于迁移历史记录，默认 dry-run。
- 唤醒规则集中在英文持久 `<channel-wake-routing>`：`@成员` 只唤醒被点名者，`^成员` 排除
  指定成员，无标记则唤醒全部其他在线成员；“只回复用户时使用 `@<username>`”和 `^` 排除
  提示也写在这里，不再在每轮 user message 或 `<turn-control>` 中重复。Aki 的
  `<channel-wait-for>` 为空，且代码层禁止 Aki 注册/渲染 `wait_other`。
- `wait_for` 的 final 兜底护栏：turn 正常结束时，如果配置的前序成员仍在 working 或有排队
  turn，akid 丢弃本次 final（不进 MQTT、群聊和频道历史），只发送 `ignored=true` 的 ended
  事件；随后注入 `origin=wait_guard` 的系统 turn，说明“这是系统提示，不是用户消息：你刚才
  没有等待应该等待的目标，这些目标是:…”。新 turn 继承原 participant 与 `wait_for` 目标，
  不消耗主动启动次数、不获得非只读执行锁，可正常补调 `wait_other`。护栏不改变并发语义，
  `wait_other` 仍是提示词驱动。
- `[NO_REPLY]` 采用包含式判定：大小写、空格、连字符和全角括号变体均识别；只要最终正文
  中包含该标记，整轮不发布正文。`wait_other` 的工具与 turn 提示同时要求模型不要在回复中
  强调等待了谁。
- 运行中消息 overlay：turn 运行期间到达的真人补充消息不再同步调用 `aki_update_turn`，
  而是原子写入 `AKI_TURN_OVERLAY_DIR/<turn_id>/<zero-padded-seq>-<uuid>.json`
  （默认 `backend_dir/aki-turn-overlays/<agent_id>`，目录 `0700`、文件 `0600`）。文件带
  稳定的 `insertion_id`、seq 和 `inserted_at`/`inserted_at_ms`，`content` 为
  `<user-insertion>` 块，不重复写 during-work note。bridge 只消费/删除文件一次，但在
  turn 结束前每次 provider 请求都会重放当前 turn 的插入集合
  `<user-insertions count=N during-work="true">`，避免 tool result 后上下文丢失。若没有
  后续 API call，未消费消息在 turn 退出时合并成一个新的用户 turn；已被消费的消息用一个
  `<user-insertions>` 文本 part 并入当前 turn 最近一条真人 user role，智能体触发的 turn
  先回滚临时 prompt/assistant/tool 消息再合并，并通过 `reload_conversation` 让下一次请求
  读到结果。`wait_guard` correction turn 的已消费插入合并到最近一条真人频道 user role，
  避免护栏打回后再生成一个重复的兜底用户 turn。合并或 reload 失败只记录错误，不把已消费
  消息退回未消费路径再次生成 turn。频道 ingress 不再调用 `aki_update_turn`；坏文件或 turn
  不匹配的文件保留并记录 warning。群聊记录在消息送达时立即追加为普通用户消息，多条插入
  不在群聊历史中合并。
- `wait_user(timeout_s)` 是频道隐藏工具：仅真人触发的 turn 可调用，只等待同一位触发者的
  下一条消息，超时参数限定 1–25 秒；等待期间的消息优先通过 `WAIT_USER_RESULT` 返回，
  不写 overlay 文件；返回后已消费消息不会重复补 turn，模型可再次调用。工具事件不写入
  频道，提示词禁止模型解释或提及等待。
- 等待期间会广播 `waitingUser=true`（presence/status 与 QoS1 `kind=wait_user` 事件）。
  有等待者时，无 `@` 的用户新消息只投递给等待者；working 成员仍可收到补充，但不额外唤醒
  空闲且非等待成员。有 `@` 时仍按点名路由。
- Tokifactor 的“还没说完”分段编辑只在本机暂存文字/表情，确认后用 `\n\n` 拼成单条普通
  协议消息发送；确认前不产生 MQTT 流量。用户消息中的 `\n\n` 在前端渲染为多个气泡。
- 频道图片：Tokifactor 支持正文加最多 4 张图片，单图 8 MiB、单条总计 16 MiB。Aki 把
  入站图片存入各 profile 的 `~/.aki/channel-media/`，频道历史和 system prompt 只记录
  `{"name","path"}`；只有当前触发 turn 的新消息图片作为 base64 多模态输入。纯图片只记录
  不唤醒。Agent 用最终正文 `#[/absolute/path]` 内嵌图片，不为此调用
  `tokifactor_send_image`。Agent 触发的临时 turn 必须通过 conversation 快照回滚，确保
  user role 中新增的多模态 base64 和后续消息全部消失。
- 内联表情：每个 profile 可从 `assets/emoji/<profile>/index.json` 获得自己的表情目录。
  akid 只把经过校验的 `name`/`desc` 注入 `<channel-policy>` 内的 `<emoji-catalog>`，
  不把图片、路径或 base64 放进 prompt。对模型公开的唯一规范格式是
  `#[name:desc]`；标签随普通正文进入频道历史、transcript 和独处记录，不触发工具、
  执行锁或多模态输入。Tokifactor 用 `scripts/sync-emoji-assets.py` 把素材缩放到最长边
  512px 后生成 Compose Resources 与 `catalog.json`；前端只按 `name` 匹配，并保留对缺失
  或错误 desc 的容错。前端在标签处切段，表情单独渲染为无气泡透明消息。
- 独处模式：`akictl solo <成员>` 通过群 control 握手。目标成员切换到由群 magic 与
  memberId 派生的 solo magic，但保持 MQTT session/client id 与订阅不变；其他成员继续
  正常运行。目标成员的群 transcript 在独处期间冻结保留，独处 transcript 单独注入
  `<solo-transcript>`，使用 `[solo] transcript_budget_tokens`。独处回复强制
  `wake=user`、移除成员 `@`、禁用 `wait_other`。目标成员自己的独处历史写入
  `<profile>.solo.jsonl`；退出后不广播、不混入群 transcript。
- 独处记忆：退出独处时把本次 `aki.solo.jsonl` 中的最终消息保存到
  `~/.aki/solo-memory/<profile>.json`（0600，原子写）。摘要通过该 profile 的
  loopback LLM proxy 生成，超时 200 秒；失败时写入确定性 fallback 并在下次启动重试。
  历史独处会话以 `<solo-memories budget_tokens="65536">` 只注入本 Agent；超预算时由
  独立人格 API 批量选择淘汰，原文归档到 MemFS `solo/<session_id>.md` 并提交 git。
  API/JSON/ID 校验失败或淘汰后仍超预算时，不修改 active 存储，当前 prompt 只渲染预算内
  最新内容。独处消息仍留在本 Agent 普通 conversation，不进入群 transcript。
- 服务：`akid@<profile>.service` 管网关，`akid-proxy@<profile>.service` 管独立
  loopback LLM proxy；akid 进程本身不监听模型端口。启动时必须等待 backend ready 并确认
  `system/init.aki_bridge`；不是本项目定制的 Letta（未广告 `aki_bridge`）会直接报错退出，
  不再启用旧的持久化 turn 路径。
- MQTT：默认使用本机 mosquitto（`127.0.0.1:1883`，同时监听 `0.0.0.0:1883` 供
  LAN/WireGuard 客户端使用）。头像按消息里的 `avatarHash` 协商：缺失或不一致才发
  `want`，收到显式 `want` 才回完整 `have`，连接成功不再广播头像。Linux
  `tokifactord` 只有在前端连接 `tokifactord.sock` 时才请求头像。
- 称呼：每个 profile 独立保存在 `<profile>.conf` 的 `[gateway] username` 键。
- 上下文预算：Aki 为 192,000 tokens；达到 90%（172,800 tokens）触发 compaction。
  该覆盖由 `akid@aki.service.d/compaction.conf` 的 `AKI_COMPACTION_RATIO=0.9` 提供，
  其他 profile 仍使用默认 0.5。
- 会话模型：单会话，固定使用 `default` conversation。唯一对话入口是 MQTT 频道；
  `akictl chat` 只是 MQTT 客户端，通过唯一 `kind=user` 身份发送消息，
  `aki.chat.*` 本地 RPC、`bin/aki` 和 GUI bridge 已移除。
- 记忆整理：`reflectionTrigger = step-count`，阈值 25；累计 25 个未反思 step 后由 reflection 将稳定信息写入 MemFS。backend 通过 `--reflection-trigger step-count --reflection-step-count 25` 显式生效。
- Compaction：默认 `sliding_window`，优先总结最早的消息，保留最近上下文。
- 桌面实况：每个 turn 采集一次桌面快照。`<live-context>` 只作为 per-turn overlay 追加在
  实际 API 请求末尾，不写回持久 system prompt。截图只放路径与元数据，默认 64K token 上限。
- Prompt 分层原则：**规则性、记忆性信息进 system prompt；时效性信息进当前 user-role /
  turn overlay。**
  - 持久 system prompt 只包含静态 base、core memory 和 MemFS memory 索引。
  - stable system overlay：`<tool-ownership> → <channel-policy> → <channel-wake-routing> →
    <channel-identity> → <channel-context roster> → <format-guard>`。
  - per-turn user prefix：`<turn-control> → <channel-transcript> → <solo-mode> →
    <solo-memories> → <solo-transcript> → <live-context>`，临时拼在当前 user role
    消息开头，不写回持久消息。
  - akid 运行时不写 `agent.system` 或 `system-prompt.json.content`；所有 Aki 运行时块
    都只通过 `turn_context.system_append` / `turn_context.user_message_prefix` 进入请求。
    没有 `aki_bridge` 的后端不允许启动。
- 专属工具：Aki 独占日程 CRUD、跨成员回复预算清零、生活账本；Fuyu 不注册这些工具。工具
  schema 过滤、Letta ownership mod、akid 运行时校验和 `<tool-ownership>` 提示词共同
  拦截错误调用。非 owner 收到带 owner 名称与 `@Owner` 转述提示的错误。
- Aki 状态：`~/.aki/schedules/aki.json`（最多 256 条，10 分钟 missed 宽限）、
  `~/.aki/ledger/aki.json`（文件 ≤16384 UTF-8 字节，单条正文 ≤1024 字节，FIFO 淘汰）、
  `~/.aki/presence/aki.json`（工作/深夜/归来触发器状态）。
- 主动触发器：logind 活动采样；连续工作 2 小时后首次唤醒、之后每小时；00:30–05:30
  每夜最多一次；空闲 45 分钟后归来唤醒，10 秒防抖且启动/重启后首次过渡抑制。
  主动提醒走频道 final 输出（`wake=user`）并镜像桌面气泡，不唤醒其他智能体。
- 长期记忆规则见 MemFS 中的 `memory-policy.md`；普通测试对话不得写入长期记忆。

## 13. GUI 与信道身份

- **首选 GUI 是 tokifactor。** Aki 与 GUI 的信道身份统一为 `(broker host, port, magic)`；
  不假设固定 IP，也不假设信道上一定有 Aki。
- tokifactor 通过加密的 `tokifactor/control` 主题发送 probe；Aki 回复 presence，并用
  status 心跳维持在线状态。控制消息不进入聊天历史、不创建 turn、不触发通知。
- `[channel] description` 就是该智能体的频道简介，最多 512 加权字符（ASCII 1、其他
  Unicode 码点 2）。control 的 `memberKind` 为 `user` 或 `agent`，缺失按 `agent`
  兼容；人类 Tokifactor 成员出现在 roster 与详情页，但不会触发 agent 专属逻辑，也不会
  被选为执行确认者。
- 频道成员的唯一 ID 是不可变 `memberId`，Name 只是显示标签。新成员为
  `mem_<uuid4hex>`；旧数据为 `legacy_<sha256("tokifactor-member-v1\\0" + NFKC(trim(lower(name))))[:32]>`。
  每个 profile 的 `userdata/identity.json` 是权威清单，`[channel] id/name/aliases` 是镜像；
  `~/.aki/identities.json` 只是本机客户端共享的别名/标签注册表，不是中心化身份服务。
  新 Name 与所有历史别名永久占用，改名不改变 memberId。control 同时发布
  `memberId/memberName`，并保留 `agentId=memberId`、`agentName=memberName` 兼容旧客户端。
  同名旧设备通过同一 legacy ID 合并；首个 source 拥有资料，后续 source 只增加在线来源。
  tokifactor 顶部显示多成员栏，频道历史和 transcript 的 `sender_id` 同样规范化为 memberId。
  `@` 仅影响各智能体的回复判断，不阻止其他智能体启动 turn。
- 每个智能体有默认 4 次“主动启动次数”：用户消息重置；其他智能体触发的 turn
  在第一次产生频道动作时扣 1，每 turn 最多 1 次；剩余次数写入 channel live context。
- KDE Plasma widget、`engine/gui_bridge.py`、`install-widget.py` 和 `bin/aki`
  已整体移除，不再保留兼容层。

### 13.1 透明桌面画布层

- `aki-layer/` 是 Qt Quick + LayerShellQt 的透明全屏画布，使用 Layer shell `overlay`
  层：位于普通应用窗口之上的最高层。表面默认为全透明，在右上角显示通知气泡，并在右侧
  垂直中部显示用户活跃状态卡。
- Layer shell 的透明 surface 默认仍会接收整个输出的输入；`aki-layer` 用
  `QWindow::setMask()`（QtWayland 映射为 `wl_surface.set_input_region`）把输入区域限制
  在各气泡矩形内，气泡之外点击会透传给桌面图标和下层窗口。
- 默认只由 Aki profile 的 `akid` 拉起；`AKI_LAYER_ENABLED=0` 可关闭，
  `AKI_LAYER_BIN` 可覆盖可执行文件路径。需要 Wayland 会话；没有
  `WAYLAND_DISPLAY` 时会跳过并记录原因。
- `akid` 监督该子进程并按退避策略重启；状态通过 `aki.health` / `aki.status` 的
  `layer` 字段公开。
- `--demo-bubbles N` 可在启动时生成样例气泡用于前端布局测试；`--notify` 仍可用于单独
  测试 `org.freedesktop.Notifications`。
- 通知气泡由 `aki-layer/qml/BubbleStack.qml` 和 `NotificationBubble.qml` 实现：右上角向下
  堆叠，默认 15 秒；鼠标悬停任意气泡时全部气泡暂停计时，并把剩余时间提升到至少 5 秒。
  未被指针划过的气泡到期后整体收出屏幕上边缘之外，不保留汇总条，也不会直接消失；上边缘
  只露出气泡的细边，多条错过消息最多叠成 3 层卡片边缘。指针靠近上边缘时气泡本身自动滑回，
  移开后重新收出，每条错过消息自带关闭按钮。主动气泡和错过消息都显示到达时间。
  角色主题定义在 `qml/Themes.js`：Aki 粉色秋叶、Fuyu 浅蓝雪花、Haru 翠绿画笔/耳机、
  Natsu 红色网格/几何。
- `aki-layer` 在 `$XDG_RUNTIME_DIR/aki-layer.sock` 提供本地控制通道：每行文本生成一个
  气泡（纯文本默认 Aki，或使用 `{"character":"Fuyu","text":"..."}`），返回
  `queued <id>`，气泡结束后广播 `dismissed <id>`。`{"kind":"presence","presence":...}`
  更新右侧状态卡，返回 `ok presence`，不会生成气泡。
- akid 后端自动把“最终消息”写入该通道，不提供模型工具：本地 turn 完成后推送 Aki 气泡；
  频道 turn 的最终回复在成功发布后推送；其他成员的 `akid.v1` 最终消息按发送者映射到
  Aki/Fuyu/Haru/Natsu 主题。最终消息先按 tokifactor 的 `splitAgentParagraphs` 规则拆分：
  空行分段、连续空行合并、围栏代码块内部不拆、代码块前后文本各自成段；每个自然段生成一个
  气泡，再按 80 字符折叠空白预览。进度、工具、审批、控制消息以及重放的积压消息不通知。
  `aki.health` / `aki.status` 的 `layer.notifier` 暴露发送计数和错误。
- 用户活跃状态卡由 `qml/PresencePanel.qml` 实现：显示 akid 判定的活跃/非活跃、连续活跃
  时长、连续未输入时长和是否达到离开阈值。akid 每次 presence 采样后推送快照；前端在两次
  采样之间本地计时，超过采样周期或 45 秒未更新时把状态标记为已过期。卡片 7 秒无悬停后
  整体向右滑出屏幕外，不保留可见边条；指针移到屏幕右缘中部时滑回。收起时只保留右缘不可见
  悬停感应区的输入区域，展开时使用卡片的最终矩形，不阻断其余桌面点击。
- `AKI_LAYER_NOTIFY_ENABLED=0` 可关闭后端推送；`AKI_LAYER_PREVIEW_CHARS` 控制预览长度；
  `AKI_LAYER_SOCKET` 同时覆盖画布控制 socket 和后端通知目标。

