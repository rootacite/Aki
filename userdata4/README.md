# Natsu userdata

`userdata4/` 是 Natsu profile 的可公开人格模板，由 `~/.aki/natsu.conf` 中的
`[gateway] userdata = userdata4` 指向。Natsu 是四季工坊隐于暗处的冷面红队大姐头与
安全架构师，负责二进制逆向、漏洞利用验证、本地渗透与安全告警响应；日常陪伴、创意和
通用代码运维分别交给 Aki、Haru 与 Fuyu。

视角分层：`persona.md` 用第二人称身份锚点，`character/voice.md` 用语气规则，
`character/relationship.md` 用关系事实与边界，`character/examples.md` 保留第一人称对话示范。

Natsu 默认称呼用户为“人类”，默认使用 `aki-natsu/deepseek-flash`，proxy 端口为 `127.0.0.1:7724`。
配置同时保留 `[model.grok-4.6]` 路由，需要时可通过 `akictl natsu model set grok-4.6`
切换；Grok 路由通过 `[upstream.xai-oauth]` 复用本机 `~/.grok/auth.json` 的 xAI OAuth 凭据。
群聊中她默认只响应显式 `@Natsu`；没有 `@` 的广播不会唤醒她，只会进入 transcript。
`[mcp] excluded` 为空，因此本机授权范围内的 recon/EDR 工具会注册给她；通用 profile
仍默认排除 `local` MCP。Natsu 没有 Haru 的生图工具，也不拥有 Aki 的日程/账本工具。
本地渗透 MCP 源码内嵌在 `vendor/mcps/`，运行时命令是
`vendor/mcps/.venv/bin/mcps`；用 `python3 scripts/install-local-mcp.py --profile natsu`
创建 venv 并写入 Natsu 的 per-agent MCP 配置。
侦察范围、端口上限、作业目录和代理环境变量都在 `vendor/mcps/config/recon.yaml`。

首次启动或运行 `scripts/bootstrap-member.py --profile natsu` 时会创建 `identity.json`，
保存 Natsu 自己的不可变 `memberId`、当前 Name、永久别名和 `kind=agent`。

同步方式：

```bash
python3 scripts/sync-persona.py --source userdata4 --agent-id <natsu-agent-id>
python3 scripts/sync-personas.py --profile natsu
```

安全边界：只处理本地、沙箱内或明确授权的目标；破坏性操作、样本执行和持久化修改必须先
取得用户授权；不把原始恶意样本、凭据或完整 exploit 写入长期记忆。
