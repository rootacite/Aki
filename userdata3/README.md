# Haru userdata

`userdata3/` 是 Haru profile 的可公开人格模板，由 `~/.aki/haru.conf` 中的
`[gateway] userdata = userdata3` 指向。Haru 是四季工坊活泼、爱恶作剧的妹妹与创意担当，
负责创意发想、视觉脑暴和氛围；代码、运维与安全交给 Fuyu、Aki 或 Natsu。

视角分层：`persona.md` 用第二人称身份锚点，`character/voice.md` 用语气规则，
`character/relationship.md` 用关系事实与边界，`character/examples.md` 保留第一人称对话示范。

Haru 已接入文本模型、MiniMax 文生图（`generate_image`）、本机 ComfyUI 生图，并有截图、
摄像头和频道发图工具。TTS 和视频尚未安装，人格和系统提示词会明确说明这一点，避免模型
虚构能力。

文生图配置在 `~/.aki/haru.conf` 的 `[upstream.minimax]` 与 `[image]`；生成文件位于
`/tmp/aki/images`，不会写入 MemFS，也不会自动附在模型上下文里。

ComfyUI 使用 `[comfyui]` 配置节和 `misc/Haru.json` 工作流。`comfyui_startup` 拉起
`startup_script`，`comfyui_generate` 只连接该工具托管的服务，`comfyui_end` 用 SIGINT
结束进程组；生成文件默认保存在 `/tmp/aki/comfyui`。

首次启动或运行 `scripts/bootstrap-member.py` 时会创建 `identity.json`，保存 Haru 自己的
不可变 `memberId`、当前 Name、永久别名和 `kind=agent`。它是该 profile 的权威身份清单；
`[channel] id/name/aliases` 只是镜像。该文件不写入 MemFS，目录移动时身份随之移动。

同步方式：

```bash
python3 scripts/sync-persona.py --source userdata3 --agent-id <haru-agent-id>
```
