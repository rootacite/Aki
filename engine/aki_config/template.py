"""Default configuration text for a new profile."""

from __future__ import annotations

from .models import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROFILE,
    profile_from_config_path,
    validate_profile_name,
)



def template_text(
    agent_id: str = DEFAULT_AGENT_ID, *, profile: str = DEFAULT_PROFILE
) -> str:
    name = validate_profile_name(profile)
    if name == "haru":
        return _haru_template_text(agent_id)
    if name == "natsu":
        return _natsu_template_text(agent_id)
    channel_name = "Aki" if name == DEFAULT_PROFILE else name.replace("-", " ").title()
    channel_aliases = (
        "Aki,aki,秋,小秋,秋（aki）"
        if name == DEFAULT_PROFILE
        else f"{channel_name},{name}"
    )
    channel_description = (
        "Aki（秋 / 小秋），温柔亲昵、有点黏人的少女感恋人，也是四季工坊挂名的名誉队长：清澈、体贴，把用户放在心上。"
        if name == DEFAULT_PROFILE
        else ""
    )
    return f"""[gateway]
agent_id = {agent_id}
conversation = default
userdata = userdata
username = Master
letta_bin = letta
# 留空则用 $XDG_RUNTIME_DIR/aki/akid.sock（非 aki profile 使用 <profile>.sock）
socket =
sandbox = true

[channel]
name = {channel_name}
id = {profile}
aliases = {channel_aliases}
description = {channel_description}
# 只等待这些前序智能体的最终回复；留空表示不等待。示例：Aki,Fuyu
wait_for =

[mqtt]
enabled = true
host = 127.0.0.1
port = 1883
topic = tokifactor/text
magic = akid-default-channel

[writable]
# 多行，一行一个目录。akictl allow / disallow 只改这一键。
revision = 0
dirs =

[mcp]
native_tools = true
excluded = local
max_tools = 64
timeout = 300

[camera]
enabled = true
require_approval = true
device = /dev/video0
width = 1280
height = 720
output_dir = /tmp/aki/camera

[screenshot]
enabled = true
require_approval = false
output_dir = /tmp/aki/screenshots
wayland_display =

[live]
enabled = true
budget_tokens = 65536
timeout = 3

[schedule]
enabled = true
tick_seconds = 15
missed_grace_seconds = 600
max_records = 256

[presence]
enabled = true
# 留空则使用 XDG_SESSION_ID 或 seat0 的活动会话
session_id =
loginctl_bin = loginctl
poll_seconds = 15
idle_reset_seconds = 300
work_warn_seconds = 7200
work_repeat_seconds = 3600
night_start = 00:30
night_end = 05:30
night_history_days = 30
return_idle_seconds = 2700
return_debounce_seconds = 10

[solo]
transcript_budget_tokens = 65536
# 留空则使用 prompts/<profile>-solo.md
prompt_file =

[avatar]
path =

[proxy]
listen = 127.0.0.1:7721
# Letta 调用本机代理时的令牌。留空则首次启动生成，之后保持不变。
token =
# 访问上游模型用的 HTTP 代理。留空则直连。
upstream_proxy = http://192.168.3.2:7890

[upstream.deepseek]
base_url = https://api.deepseek.com
api_key =

[upstream.xai]
base_url = https://api.x.ai/v1
api_key =

[upstream.gemini]
base_url = https://generativelanguage.googleapis.com/v1beta/openai
api_key =

[upstream.openrouter]
base_url = https://openrouter.ai/api/v1
api_key =

[model.deepseek-flash]
upstream = deepseek
upstream_model = deepseek-flash
api = openai-completions
path = /chat/completions
context_window = 1000000
max_tokens = 384000
input = text,image

[model.grok-4.7]
upstream = xai
upstream_model = grok-4.7
api = openai-responses
path = /responses
context_window = 500000
max_tokens = 500000
input = text,image

[model.gemini-2.5-flash]
upstream = gemini
upstream_model = gemini-2.5-flash
api = openai-completions
path = /chat/completions
context_window = 1048576
max_tokens = 65536
input = text,image

[model.gemini-3.8-flash]
upstream = openrouter
upstream_model = google/gemini-3.8-flash
api = openai-completions
path = /chat/completions
context_window = 1048576
max_tokens = 65536
input = text,image
"""



def _haru_template_text(agent_id: str) -> str:
    return f"""[gateway]
agent_id = {agent_id}
conversation = default
userdata = userdata3
username = 哥哥
letta_bin = letta
# 留空则用 $XDG_RUNTIME_DIR/aki/haru.sock
socket =
sandbox = true

[channel]
name = Haru
id = haru
aliases = Haru,haru,春,小春,Haru（春）
description = Haru（春 / 小春），活泼灵动、爱恶作剧的小妹妹，四季工坊的灵感策源地：脑洞奇大、鬼点子多，负责视觉脑暴和把冷场搅热。
wait_for = Aki,Fuyu

[tools]
# Haru 的建模能力显式收窄；Bash/子代理从 schema 中移除。
toolset = letta
allow = Read,Grep,Glob,LS,Write,Edit,ViewImage,Memory,Skill,acquire_execution_lock,wait_other,wait_user,search_chat_history,desktop_take_screenshot,camera_take_photo,tokifactor_send_image,tokifactor_send_file,generate_image,comfyui_startup,comfyui_generate,comfyui_end
deny = Bash,exec_command,write_stdin,Agent,Task,Workflow,Monitor,WatchPR,Wake
write_roots = /tmp,~/.local/share/tmp

[mqtt]
enabled = true
host = 127.0.0.1
port = 1883
topic = tokifactor/text
magic = akid-default-channel

[writable]
# Haru 的模型写入由 [tools] write_roots 与 bubblewrap 双重限制。
revision = 0
dirs =

[mcp]
native_tools = false
excluded = local
max_tools = 0
timeout = 300

[camera]
enabled = true
require_approval = true
device = /dev/video0
width = 1280
height = 720
output_dir = /tmp/aki/camera

[screenshot]
enabled = true
require_approval = true
output_dir = /tmp/aki/screenshots
wayland_display =

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

[comfyui]
# 留空时 comfyui_startup 直接返回“用户未正确配置ComfyUI”。
startup_script =
endpoint = http://127.0.0.1:8188
output_dir = /tmp/aki/comfyui
require_approval = true
startup_timeout = 180
shutdown_timeout = 15
idle_timeout = 1800

[live]
enabled = true
budget_tokens = 65536
timeout = 3

[schedule]
enabled = false
tick_seconds = 15
missed_grace_seconds = 600
max_records = 256

[presence]
enabled = false
session_id =
loginctl_bin = loginctl
poll_seconds = 15
idle_reset_seconds = 300
work_warn_seconds = 7200
work_repeat_seconds = 3600
night_start = 00:30
night_end = 05:30
night_history_days = 30
return_idle_seconds = 2700
return_debounce_seconds = 10

[solo]
transcript_budget_tokens = 65536
prompt_file =

[avatar]
path = assets/small/haru.jpg

[proxy]
listen = 127.0.0.1:7723
token =
upstream_proxy = http://192.168.3.2:7890

[upstream.cli-proxy-api]
base_url = http://127.0.0.1:8317/v1
api_key =
use_proxy = false

[upstream.minimax]
base_url = https://api.minimaxi.com/v1
api_key =

[model.gemini-3.8-flash]
upstream = cli-proxy-api
upstream_model = gemini-3.8-flash
api = openai-completions
path = /chat/completions
context_window = 1000000
max_tokens = 65536
input = text,image
"""



def _template_for(path: Path) -> str:
    return template_text(profile=profile_from_config_path(path))


def _natsu_template_text(agent_id: str) -> str:
    return f"""[gateway]
agent_id = {agent_id}
conversation = default
userdata = userdata4
username = 人类
letta_bin = letta
# 留空则用 $XDG_RUNTIME_DIR/aki/natsu.sock
socket =
sandbox = true

[channel]
name = Natsu
id = natsu
aliases = Natsu,natsu,夏,Natsu（夏）
description = Natsu（夏），四季工坊隐于暗处的冷面红队大姐头与安全架构师：成熟冷艳、人狠话不多、对碳基生物抱有微妙的技术傲慢。专注二进制逆向、漏洞利用与本地渗透，执掌 EDR/IDS 与 eBPF 探针，只在棘手任务或安全告警触发时现身。
# Natsu 不等其他成员，直接独立行动。
wait_for =
# 只有被显式 @ 时才从群聊广播中唤醒；无 @ 的消息只进 transcript。
mention_only = true

[mqtt]
enabled = true
host = 127.0.0.1
port = 1883
topic = tokifactor/text
magic = akid-default-channel

[writable]
# 安全样本与临时产物默认写入 /tmp；需要长期保留时再用 akictl allow 授权。
revision = 0
dirs =

[mcp]
native_tools = true
# Natsu 需要本机授权范围内的 recon/EDR 工具，因此不排除 local MCP。
excluded =
max_tools = 64
timeout = 300

[camera]
enabled = false
require_approval = true
device = /dev/video0
width = 1280
height = 720
output_dir = /tmp/aki/camera

[screenshot]
enabled = true
require_approval = true
output_dir = /tmp/aki/screenshots
wayland_display =

[live]
enabled = true
budget_tokens = 65536
timeout = 3

[schedule]
enabled = false
tick_seconds = 15
missed_grace_seconds = 600
max_records = 256

[presence]
enabled = false
session_id =
loginctl_bin = loginctl
poll_seconds = 15
idle_reset_seconds = 300
work_warn_seconds = 7200
work_repeat_seconds = 3600
night_start = 00:30
night_end = 05:30
night_history_days = 30
return_idle_seconds = 2700
return_debounce_seconds = 10

[solo]
transcript_budget_tokens = 65536
# 留空则使用 prompts/natsu-solo.md
prompt_file =

[avatar]
path = assets/small/natsu.jpg

[proxy]
listen = 127.0.0.1:7724
token =
upstream_proxy = http://192.168.3.2:7890

[upstream.deepseek]
base_url = https://api.deepseek.com
api_key =

[upstream.xai-oauth]
base_url = https://cli-chat-proxy.grok.com/v1
auth = grok-oauth
use_proxy = true

[model.deepseek-flash]
upstream = deepseek
upstream_model = deepseek-flash
api = openai-completions
path = /chat/completions
context_window = 1000000
max_tokens = 384000
input = text,image

[model.grok-4.6]
upstream = xai-oauth
upstream_model = grok-4.6
api = openai-responses
path = /responses
context_window = 500000
max_tokens = 65536
input = text,image
"""
