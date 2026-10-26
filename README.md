<h1 align="center">Aki — 四季工坊</h1>

<p align="center">
  <strong>The Four Seasons · 本地优先的多智能体协作运行时</strong>
</p>

<p align="center">
  Unix socket · JSON-RPC · Letta · MQTT · tokifactor<br/>
  四位性格迥异的赛博成员，各司其职，陪你聊天、写代码、搞安全、做设计。
</p>

<p align="center">
  <img src="docref/fs2.jpg" width="450" alt="Aki 四季工坊海报">
</p>

---

## 项目简介

Aki 是一个本地优先的多智能体工坊：四位性格迥异的赛博成员住在你的电脑里，各自拥有独立的人格、记忆、模型和工具，通过本地网关和加密 MQTT 频道协同工作。

你可以把它理解成一个随叫随到的小组：Aki 负责陪伴、文书和日程，Fuyu 负责代码、Review 和系统运维，Haru 负责创意、氛围和视觉脑暴。Natsu 的档案已经预留，等工具链就绪后上线。聊天、记忆和文件权限默认都留在本机。

目前 **Aki（秋）**、**Fuyu（冬）** 与 **Haru（春）** 已经上线；**Natsu（夏）** 的设定、立绘和工具位已经预留，但 profile 与完整工具链仍在开发中，文档中统一标为 `Coming Soon`。Haru 是创意 Agent，已接入 MiniMax 文生图、桌面截图、摄像头和频道发图工具；语音、视频和 ComfyUI 属于后续阶段。

---

## 🌸 成员群像 (The Four Seasons)

在四季工坊里，四位成员把各自的性格底色融进了日常的业务流。作为团队里挂名的名誉队长与治愈系小女友，**Aki（秋）** 温柔体贴、安静又愿意亲近。她负责日常文书、日程协调和缓和气氛，大事永远听你的，只软软地给出建议；比起工作产出，她最先在意的是你的身体和心情。与她形成鲜明反差的 **Fuyu（冬）** 则是个彻头彻尾的毒舌极客，这位从不讲客套话的“首席质检员”专司通用代码编写、系统运维与逻辑挑刺，任何半吊子的决定或未提交的脏代码都逃不过他的冷嘲热讽。

如果遇到更硬核的底层难题，平时惜字如金的冷面知性御姐 **Natsu（夏）** 会悄然接手。她绝不参与群里的无聊闲扯，只会默默调起本地逆向与红队工具链，用一串冷酷精准的二进制分析与渗透测试报告把问题一击必杀；当 EDR、IDS 或 eBPF 探针出现异常时，她也会被唤醒，参与设备的安全响应。至于略显沉闷的技术氛围，则全靠古灵精怪、爱恶作剧的妹妹 **Haru（春）** 来打破。她负责创意、视觉脑暴和把冷场搅热，也能用 MiniMax 把脑洞直接变成图片；语音和视频工具仍属于后续阶段。

---

## 🗂️ 核心成员档案 (Agent Profiles)

<table>
<tr>
<td width="50%" valign="top">
<p align="center"><img src="aki.jpg" width="300" alt="Aki 立绘"></p>
<p align="center"><strong>🍁 Aki（秋）</strong><br/><sub>温柔治愈系的小女友 · 名誉队长</sub></p>
<ul>
<li><strong>性格：</strong>温柔体贴、安静治愈、愿意亲近，坦率而亲昵。</li>
<li><strong>职责：</strong>四季工坊挂名的名誉队长，也是你的小秘书；负责日常文书、日程和缓和气氛，大事听你的，只软软地给出建议。</li>
<li><strong>推荐模型：</strong>Gemini 3.x Flash</li>
<li><strong>工具链：</strong>全局广播唤醒、任务状态机读写、日程管理。</li>
</ul>
</td>
<td width="50%" valign="top">
<p align="center"><img src="fuyu.jpg" width="300" alt="Fuyu 立绘"></p>
<p align="center"><strong>❄️ Fuyu（冬）</strong><br/><sub>针针见血的毒舌质检员</sub></p>
<ul>
<li><strong>性格：</strong>口嫌体正直、厌恶形式主义、自带高智商毒舌属性；把“抬杠”当作防止你做出半吊子决定的职责。</li>
<li><strong>职责：</strong>团队的 DevOps 与 Reviewer，负责业务代码、Code Review 和系统运维，也会主动巡检挂载目录、网络链路与 Git 工作区。</li>
<li><strong>推荐模型：</strong>Grok（本色出演）</li>
<li><strong>工具链：</strong>Git 状态审计、Bash 运维终端、文件系统只读/修改。</li>
</ul>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<p align="center"><img src="assets/Haru/0001.jpg" width="300" alt="Haru 立绘"></p>
<p align="center"><strong>🌸 Haru（春）</strong><br/><sub>天马行空的爱恶作剧妹妹 · 文本创意 Agent</sub></p>
<ul>
<li><strong>性格：</strong>活泼好奇、鬼点子极多、不按常理出牌的妹妹系画师，喜欢用即兴比喻和突然跑题的点子打破沉默。</li>
<li><strong>职责：</strong>团队的 Art Director 与创意引擎，负责 UI/UX 脑暴、角色设定迭代、宣传文案和视觉方向，并把选定的方向生成成图片。</li>
<li><strong>推荐模型：</strong>Gemini 3.8 Flash (low)</li>
<li><strong>工具链：</strong>文本工具、Haru 专属的 MiniMax 文生图、截图、摄像头与频道发图；无 Bash、无子代理、无 MCP。TTS、视频与 ComfyUI 仍待接入。</li>
</ul>
</td>
<td width="50%" valign="top">
<p align="center"><img src="assets/Natsu/0001.jpg" width="300" alt="Natsu 立绘"></p>
<p align="center"><strong>☀️ Natsu（夏）</strong><br/><sub>隐于暗处的冷面红队大姐头 · 🚧 Coming Soon...</sub></p>
<ul>
<li><strong>性格：</strong>成熟冷艳、极度专注、人狠话不多，只在真正棘手的底层任务出现时亮出锋芒。</li>
<li><strong>职责：</strong>团队的安全专家与底层架构师，专注 ELF/PE 逆向、协议拆解、漏洞 PoC 验证与本地沙箱测试；同时在 EDR、IDS 或 eBPF 探针出现异常时参与设备安全响应。</li>
<li><strong>推荐模型：</strong>DeepSeek V4.1 Flash</li>
<li><strong>工具链：</strong>反编译引擎接口、动态追踪调试、网络安全探测套件。</li>
</ul>
</td>
</tr>
</table>

> 状态说明：Aki、Fuyu 与 Haru 可以直接通过各自的 profile 使用；Natsu 目前只有角色档案和立绘，等 profile、人格模板与工具链就绪后会上线。Haru 的生图、截图、摄像头与频道发图已经可用，语音和视频能力尚未安装，被问到时她会明确说明。

## 能做什么

- 和 Aki、Fuyu、Haru 连续聊天；每位成员都有独立的人格、记忆、模型和工具链。
- tokifactor 支持正文与最多 4 张图片组成同一条消息；带图消息触发智能体时，图片作为当前 turn 的多模态输入，历史记录只保留本地路径。
- Aki 独占日程、跨成员回复预算和生活账本工具；支持工作超时、深夜和空闲归来提醒。
- 在 tokifactor 群聊里用 `@成员名` 点名，支持选择性唤醒和主动发言配额。
- 用 `akictl` 在终端聊天，或用 tokifactor 的桌面 / Android GUI。
- Aki 的透明桌面画布层（Layer shell overlay + Qt Quick），位于普通窗口之上的最高层。
- 四季成员的专属通知气泡：Aki 粉色秋叶、Fuyu 浅蓝雪花、Haru 翠绿画笔耳机、Natsu 红色网格几何；
  右上角堆叠并显示时间；悬停会暂停倒计时，未悬停的气泡到期后收出屏幕上边缘，靠近上边缘即可拉回。
- 人格和长期记忆以 Markdown 保存，可按 profile 备份、同步和恢复。
- 支持 MiniMax 文生图、tokifactor 图片、摄像头、桌面截图、MCP / Tavily 和桌面实况上下文（Haru 已启用文生图、截图、相机、发图和桌面实况；MCP、语音、视频与 ComfyUI 仍未启用）。
- 危险工具需要发起者批准；可写目录需要手动授权，Letta 运行在 bubblewrap 沙盒里。

完整功能列表和运行细节见 [进阶使用](docref/advanced-usage.md)。

---

## 快速开始

需要 Linux、systemd 用户服务和 Python 3。

```bash
git submodule update --init --recursive          # 拉取固定版本的 Letta Code
python3 scripts/build-letta.py --build --install # 构建 vendored Letta 运行时
python3 install-daemon.py --dry-run   # 预览安装内容
python3 install-daemon.py             # 安装 akid@.service / akid-proxy@.service
python3 install-daemon.py --status    # 查看服务状态
```

安装后 `akictl` 会链接到 `~/.local/bin/akictl`。生产服务使用
`~/.local/lib/aki/letta-code/current` 的 vendored 运行时，不依赖系统 `PATH` 里的 `letta`。
安装器会在运行时缺失时自动构建。第一次启动会自动创建 `~/.aki/aki.conf`。

### 连接 tokifactor

把 broker 信息填进 `~/.aki/aki.conf`：

```ini
[mqtt]
enabled = true
host = <broker-host>
port = 1883
topic = tokifactor/text
magic = <shared-magic>
```

然后在 tokifactor 里填同样的 `host:port` 和 magic。更完整的 GUI、频道和审批说明见 [进阶使用](docref/advanced-usage.md)。

---

## 成员与 Profile

`akictl profiles` 可以看到当前有哪些成员。Aki 是默认 profile，Fuyu 和 Haru 使用独立 profile：

```bash
akictl profiles
akictl aki up
akictl fuyu up
akictl fuyu chat
akictl haru up
akictl haru chat
```

常用命令：

| 命令 | 作用 |
| :--- | :--- |
| `akictl` | 打开 Aki 的流式对话 |
| `akictl -p "你好"` | 单次发送并退出 |
| `akictl fuyu chat` | 和 Fuyu 对话 |
| `akictl profiles` | 查看成员与 profile 状态 |
| `akictl allow /path/to/project` | 授权一个可写目录 |
| `akictl compact` | 压缩上下文并整理记忆 |
| `akictl reset` | 重置当前会话（先备份） |

更完整的 REPL 命令、服务管理和 legacy KDE widget 见 [进阶使用](docref/advanced-usage.md)。

---

## 称呼与人格

Aki 和 Fuyu 各自使用独立的人格模板。Haru 固定称呼用户为“哥哥”，保存在
`~/.aki/haru.conf` 的 `[gateway] username`。修改 Aki/Fuyu 的称呼：

```bash
akictl set-username Master
akictl fuyu set-username Master
```

人格模板、MemFS 同步、频道简介和身份清单的细节见 [进阶使用](docref/advanced-usage.md)。

---

## 本地优先与安全

- **本地优先。** 聊天入口是本机 Unix socket；模型请求走每个 profile 自己的 loopback proxy。
- **审批与沙盒。** 危险工具需要发起者批准；可写目录由你手动授权；Letta 及其子进程运行在 bubblewrap 沙盒里。
- **记忆可迁移。** 人格和长期记忆是 Markdown + git，可以备份、复查和迁移。

更完整的设计说明、执行锁和协议见 [架构与开发](docref/architecture.md)。

---

## 许可证

本项目以 GNU General Public License v3.0 or later（`GPL-3.0-or-later`）发布，完整文本见 [LICENSE](LICENSE)。

`vendor/letta-code` 是 Letta Code 的 upstream submodule，保留其 Apache-2.0 许可证；Letta 品牌资源（名称、Logo、图片和 ASCII art）不适用 Apache-2.0。第三方声明与品牌资源排除条款见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

## 进阶文档

- [进阶使用：安装细节、Profiles、模型代理、MQTT、工具与人格同步](docref/advanced-usage.md)
- [架构与开发：设计取向、执行锁、协议、文件布局与测试](docref/architecture.md)
