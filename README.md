<h1 align="center">Aki — 四季工坊</h1>

<p align="center">
  <strong>The Four Seasons · 本地优先的多智能体协作运行时</strong>
</p>

<p align="center">
  Unix socket · JSON-RPC · Letta · MQTT · tokifactor<br/>
  四位性格迥异的赛博成员，各司其职，陪你聊天、写代码、搞安全、做设计。
</p>

<p align="center">
  <img src="docref/fs.png" width="450" alt="Aki 四季工坊海报">
</p>

---

## 项目简介

Aki 是一个本地优先的多智能体工坊：四位性格迥异的赛博成员住在你的电脑里，各自拥有独立的人格、记忆、模型和工具，通过本地网关和加密 MQTT 频道协同工作。

你可以把它理解成一个随叫随到的小组：Aki 负责陪伴、文书和日程，Fuyu 负责代码、Review 和系统运维；Natsu 与 Haru 的档案已经预留，等工具链就绪后上线。聊天、记忆和文件权限默认都留在本机。

目前 **Aki（秋）** 与 **Fuyu（冬）** 已经上线；**Natsu（夏）** 与 **Haru（春）** 的设定、立绘和工具位已经预留，但 profile 与完整工具链仍在开发中，文档中统一标为 `Coming Soon`。

---

## 🌸 成员群像 (The Four Seasons)

在四季工坊里，四位成员把各自的性格底色融进了日常的业务流。作为团队里挂名的名誉队长与治愈系小女友，**Aki（秋）** 温柔体贴、安静又愿意亲近。她负责日常文书、日程协调和缓和气氛，大事永远听你的，只软软地给出建议；比起工作产出，她最先在意的是你的身体和心情。与她形成鲜明反差的 **Fuyu（冬）** 则是个彻头彻尾的毒舌极客，这位从不讲客套话的“首席质检员”专司通用代码编写、系统运维与逻辑挑刺，任何半吊子的决定或未提交的脏代码都逃不过他的冷嘲热讽。

如果遇到更硬核的底层难题，平时惜字如金的冷面知性御姐 **Natsu（夏）** 会悄然接手。她绝不参与群里的无聊闲扯，只会默默调起本地逆向与红队工具链，用一串冷酷精准的二进制分析与渗透测试报告把问题一击必杀；当 EDR、IDS 或 eBPF 探针出现异常时，她也会被唤醒，参与设备的安全响应。至于略显沉闷的技术氛围，则全靠古灵精怪的小恶魔妹妹 **Haru（春）** 来打破。作为团队里唯一独占生图、语音与视频外设的多模态灵感缪斯，她随时准备用天马行空的创意设计和视听作品，为整个群聊注入鲜活跳跃的色彩。

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
<p align="center"><strong>🌸 Haru（春）</strong><br/><sub>天马行空的多模态小恶魔 · 🚧 Coming Soon...</sub></p>
<ul>
<li><strong>性格：</strong>活泼好奇、鬼点子极多、不按常理出牌的妹妹系画师，喜欢用即兴画作或语音打破沉默。</li>
<li><strong>职责：</strong>团队的 Art Director 与创意引擎，负责 UI/UX 脑暴、角色设定迭代、宣传文案和多模态素材生成。</li>
<li><strong>推荐模型：</strong>MiniMax</li>
<li><strong>工具链：</strong>图像生成管线、TTS 语音合成、视频渲染引擎。</li>
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

> 状态说明：Aki 与 Fuyu 可以直接通过各自的 profile 使用；Haru 与 Natsu 目前只有角色档案和立绘，等 profile、人格模板与工具链就绪后会上线。

## 能做什么

- 和 Aki、Fuyu 连续聊天；每位成员都有独立的人格、记忆、模型和工具链。
- 在 tokifactor 群聊里用 `@成员名` 点名，支持选择性唤醒和主动发言配额。
- 用 `akictl` 在终端聊天，或用 tokifactor 的桌面 / Android GUI。
- Aki 的透明桌面画布层（Layer shell overlay + Qt Quick），位于普通窗口之上的最高层。
- 四季成员的专属通知气泡：Aki 粉色秋叶、Fuyu 浅蓝雪花、Haru 翠绿画笔耳机、Natsu 红色网格几何；
  右上角堆叠、15 秒生命周期，悬停任意气泡会暂停全部倒计时并保底 5 秒。
- 人格和长期记忆以 Markdown 保存，可按 profile 备份、同步和恢复。
- 支持 tokifactor 图片、摄像头、桌面截图、MCP / Tavily 和桌面实况上下文。
- 危险工具需要发起者批准；可写目录需要手动授权，Letta 运行在 bubblewrap 沙盒里。

完整功能列表和运行细节见 [进阶使用](docref/advanced-usage.md)。

---

## 快速开始

需要 Linux、systemd 用户服务和 Python 3。

```bash
python3 install-daemon.py --dry-run   # 预览安装内容
python3 install-daemon.py             # 安装 akid@.service / akid-proxy@.service
python3 install-daemon.py --status    # 查看服务状态
```

安装后 `akictl` 会链接到 `~/.local/bin/akictl`。第一次启动会自动创建 `~/.aki/aki.conf`。

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

`akictl profiles` 可以看到当前有哪些成员。Aki 是默认 profile，Fuyu 使用独立 profile：

```bash
akictl profiles
akictl aki up
akictl fuyu up
akictl fuyu chat
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

Aki 和 Fuyu 各自使用独立的人格模板。修改称呼：

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

## 进阶文档

- [进阶使用：安装细节、Profiles、模型代理、MQTT、工具与人格同步](docref/advanced-usage.md)
- [架构与开发：设计取向、执行锁、协议、文件布局与测试](docref/architecture.md)
