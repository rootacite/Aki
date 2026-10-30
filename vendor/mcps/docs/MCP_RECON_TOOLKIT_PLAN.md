# MCP Recon Toolkit 扩展开发计划

> 给本地 Agent 的执行说明书。人类只监工：合 scope、合安全边界、合验收。
> 基座：已有 MCP server 项目。本计划是在其上扩展「探测工具集」，不是重写框架。
> 范围冻结：第一版只做文中「最小集 + ABC 列表中已圈定的工具」。不做 sqlmap/hydra/msf/任意 shell。

---

## 0. 开工前（Agent 必做，做完停下来等人类点头）

### 0.1 读现有基座

找出并记录（写进 `docs/recon-toolkit/BASELINE.md`）：

- MCP 框架：FastMCP / 官方 SDK / 自研？入口文件？stdio 还是 SSE？
- 现有 tool 注册方式（装饰器、registry、yaml）
- 现有进程执行封装（有没有 `run_cmd`、超时、日志、job）
- 现有配置/密钥加载方式
- 测试怎么跑
- 语言与包管理（Python uv/poetry？Go？Node？）

**硬性约束：复用基座的进程执行、日志、配置，禁止再造一套 subprocess。**
若基座没有安全的 `exec`（无 `shell=True`、有 timeout、有 cwd/env 白名单），先补这一层，再挂工具。

### 0.2 本机工具探测

写脚本 `scripts/check_recon_bins.sh`，检测 PATH 中：

必装（缺则在 BASELINE.md 标明，对应 tool 注册为 unavailable，不要直接 crash 整个 server）：

- subfinder, dnsx, naabu, httpx, nuclei, nmap, whatweb, ffuf, jshunter, restringer

建议有（缺则 tool 返回明确 error）：

- amass, bbot, katana, wafw00f, masscan, theHarvester, gau, waybackurls

`restringer` 是 Node CLI（`npm i -g restringer`，需要 Node 20+），不是 pacman 里的常见包。`jshunter` 指定 **github.com/cc1a2b/jshunter**（Go：`jshunter -u/-f/-l`）。PATH 上若撞到 PyPI 那个同名 mass-scanner，以 `jshunter -h` 是否支持 `-u/-f/-s/-ep` 为准；对不上就在 BASELINE 里标明，不要包错二进制。

记录版本号。Arch + BlackArch：用 `pacman -Qi` / `command -v`。

### 0.3 人类确认点 A

停。把 BASELINE.md 给人类看。人类确认：

- 框架判断对不对
- 是否允许 MCP 进程提权跑 SYN scan（naabu/nmap -sS）
- scope 文件放哪
- 是否允许 restringer 的 unsafe/isolated-vm 反混淆（默认可跑，但必须有文件大小与超时上限）
- 本机 `jshunter` 到底是 cc1a2b 那份还是别的同名工具

---

## 1. 目标与非目标

### 目标

在现有 MCP server 上增加一组 **recon tools**，让 agent 能按阶段做：

被动子域 → DNS 解析 → 端口发现 → HTTP 探活 → 指纹 → JS 资产分析（jshunter / restringer）→ 轻量漏洞模板 → 受控 fuzz

并提供 1～2 个编排工具减少乱序调用。

### 非目标（禁止做）

- 通用 `run_shell` / `extra_args: str` 透传任意 CLI
- sqlmap、hydra、metasploit、burp 全量、nuclei `-code`、自定义模板任意路径
- 默认 kitchen-sink bbot
- 默认 nmap `-A`、默认全端口、默认 nuclei 全模板
- `shell=True`
- 把 API key 放进 tool 参数

---

## 2. 目录约定（按基座风格微调，不要另起炉灶）

建议（Python FastMCP 示例，若基座不同则映射过去）：

```
<repo>/
  src/<pkg>/
    tools/
      recon/
        __init__.py          # 注册所有 recon tools
        common.py            # argv builder, scope check, output truncate
        bins.py              # 二进制路径与 availability
        jobs.py              # 长任务（若基座已有 job，则只适配）
        scope.py
        tools_subfinder.py
        tools_amass.py
        tools_bbot.py
        tools_dnsx.py
        tools_jshunter.py
        tools_restringer.py
        tools_naabu.py
        tools_nmap.py
        tools_httpx.py
        tools_whatweb.py
        tools_wafw00f.py
        tools_katana.py
        tools_ffuf.py
        tools_nuclei.py
        workflows.py         # recon_domain_passive / recon_web_active / recon_js_analyze
  config/
    recon.yaml
    wordlists.txt            # 允许的 wordlist 路径前缀
  docs/recon-toolkit/
    BASELINE.md
    TOOLS.md                 # 每个 tool 的 description 最终稿
  tests/recon/
    test_argv_builders.py
    test_scope.py
    test_truncate.py
    test_unavailable_bin.py
  scripts/
    check_recon_bins.sh
```

每个 CLI 一个模块，禁止把全部 flag 堆进一个 2000 行文件。

---

## 3. 公共层（先写这一层，再写具体 tool）

### 3.1 `scope.py`

- 配置：`allowed_domains: []`、`allowed_cidrs: []`、`allow_private: bool`（默认 false 除非显式）
- `assert_in_scope(target: str) -> None`
- 支持：hostname、FQDN、IPv4/IPv6、CIDR、`host:port`、URL
- 子域：`foo.example.com` 仅当 `example.com` 在 allowed_domains（或配置 `allow_subdomains: true`，默认 true）
- 失败抛出明确错误，不要静默扫描

### 3.2 `common.py`

统一执行：

```
run_recon(argv: list[str], *, timeout_s: int, max_output_bytes: int, input_text: str | None) -> RunResult
```

`RunResult`: `{ok, exit_code, stdout, stderr, truncated, duration_ms, argv_redacted}`

规则：

- 只接受 list argv，禁止字符串拼命令
- 超时杀进程组
- stdout/stderr 截断（默认 256KB，可配）
- 日志里 redact 环境变量和 API key
- 返回给 LLM 的文本再做 `max_lines`（默认 200 行）+ 必要时 JSON 摘要

### 3.3 `bins.py`

```
BIN = {"subfinder": "subfinder", ...}
def require(name) -> str   # which，找不到返回 ToolError
```

### 3.4 配置 `config/recon.yaml`

```yaml
scope:
  allowed_domains: []
  allowed_cidrs: []
  allow_private: false
  allow_subdomains: true
limits:
  default_timeout_s: 120
  long_timeout_s: 900
  max_hosts: 256
  max_output_bytes: 262144
  max_lines_to_model: 200
  naabu_default_top_ports: 100
  naabu_max_rate: 2000
  ffuf_max_threads: 20
  nuclei_max_concurrency: 25
  jshunter_max_urls: 50
  jshunter_max_threads: 5
  restringer_max_input_bytes: 1048576
  restringer_max_iterations: 10
  js_workspace: /tmp/mcp-recon-js
wordlists:
  allowed_prefixes:
    - /usr/share/seclists/
    - /usr/share/wordlists/
    - /usr/share/ffuf/
jobs:
  enabled: true
  dir: /tmp/mcp-recon-jobs
```

密钥：不在 tool 参数里传。BBOT / amass 的 key 走各自官方 config。本版不集成 Shodan。

JS 工件：下载的 `.js` 与 restringer 输出只允许写在 `js_workspace`（按 job_id 分子目录），禁止用户指定任意输出路径。

### 3.5 长任务

若命令预计 > 60s（amass enum、bbot、nuclei_vuln、大 ffuf、批量 jshunter、restringer 大文件、编排 workflow）：

- 立即返回 `{job_id, status: "running"}`
- 另提供 `recon_job_status(job_id)`、`recon_job_logs(job_id, tail=80)`、`recon_job_cancel(job_id)`
- 若基座已有 job 系统，适配它，不要第二套

人类确认点 B：公共层 + 配置 schema 写完跑单测后再挂 CLI。

---

## 4. 工具清单、schema、默认值（按实现顺序）

实现顺序 = 依赖顺序。每完成一个 tool：单测 argv + 若本机有二进制则一次 smoke（对 example.com 或配置的 lab 目标，**禁止对随机公网扫**）。

通用参数（能抽就抽，不要每个 tool 复制）：

- `timeout_s: int | None`
- `json_output: bool = true`（工具支持时）

所有 description 必须含：做什么 / 何时用 / 何时不用 / 副作用 / 返回什么。

---

### Phase 1 — 被动 + 解析（无/低对目标业务端口干扰）

#### T1 `subfinder_enum`

- domain: str (required)
- all_sources: bool = false
- timeout_s: int = 120

argv 核心：`subfinder -d {domain} -silent -oJ`（或 `-json`，以本机版本为准，先 `subfinder -h`）

Description 草案：

> 被动子域枚举。查询公开源，不向目标业务端口发包。recon 第一步。需要图数据库或主动爆破时改用 amass/bbot。返回去重 hostname 列表。

#### T2 `dnsx_resolve`

- hosts: list[str] (required，上限 max_hosts)
- records: list[str] = ["A","AAAA","CNAME"]
- timeout_s: int = 60

> 批量解析与 CDN/通配提示。端口扫描前调用，避免同一 IP 重复扫。

#### T3 `amass_passive`

- domain: str
- timeout_s: int = 300 → 走 job

> Amass 被动枚举。比 subfinder 慢而全。主动爆破用 amass_active（本阶段可先不做 amass_active，标 TODO）。

#### T4 `amass_intel`（可与 T3 同文件）

- domain: str | None
- org: str | None
- asn: int | None（三者至少一）

> 组织/ASN/根域被动情报，用于定 scope，不是扫端口。

#### T5 `bbot_scan`

- target: str
- preset: str（enum: `subdomain-enum` | `cloud-enum` | `email-enum`）
- passive_only: bool = true
- timeout_s: int = 600 → job

禁止 preset=`kitchen-sink`。passive_only 时加官方被动限制 flag（先查本机 `bbot -h` / 文档，不要猜错 flag）。

输出：按 event type 计数 + 每种最多 N 条样本，不要整份日志。

Phase 1 完成标准：对允许域跑 subfinder+dnsx 有 JSON 结果；无二进制时 tool 报错清晰。

---

### Phase 2 — 端口 + HTTP

#### T6 `naabu_scan`

- targets: list[str]
- top_ports: int = 100（允许 100/1000）
- ports: str | None（与 top_ports 互斥）
- exclude_cdn: bool = true
- rate: int = 1000（封顶配置项）
- aggressive: bool = false
- scan_type: `syn` | `connect` = connect（无 root 时强制 connect）

规则：`ports` 含全端口或 top_ports=1000 必须 `aggressive=true`，否则拒绝。
输出：`host:port` 列表。

> 快速端口发现。不做服务识别（用 nmap）不做 HTTP（用 httpx）。

#### T7 `nmap_scan`

- targets: list[str]（数量上限，如 16）
- ports: str | None
- service_version: bool = true
- default_scripts: bool = false
- scripts: list[str] | None（白名单前缀，如 `http-`、`ssl-`，禁止 `*` 任意）
- timing: `T2`|`T3`|`T4` = T3
- skip_host_discovery: bool = true（对已探活目标）

禁止：`-A`、`--script vuln` 默认开、输入超大 CIDR（/16 且 host 数超 max 则拒）。
优先 `-oX` 或 `-oJ` 再解析成精简结构。

> 对已缩小目标做服务识别。不要用它做大网段发现。

#### T8 `httpx_probe`

- targets: list[str]（host、host:port、URL）
- follow_redirects: bool = true
- tech_detect: bool = true
- timeout_s: int = 90

输出字段尽量结构化：url, status, title, webserver, tech, cdn, content_length, host, port。

> HTTP 探活与基础指纹。nuclei/ffuf/whatweb/jshunter 的前置过滤。尽量打开 JS 相关探测（本机 httpx 若支持 `-js`/`-include-body` 之类先查 `-h`，没有就别编 flag）。

Phase 2 完成标准：naabu 默认 top100；httpx 能吃 naabu 输出；nmap 只接受小目标。

---

### Phase 3 — 指纹 / 爬 / JS 前中期分析

#### T9 `whatweb_fingerprint`

- urls: list[str]（上限 20）
- aggression: int = 1（允许 1–3，默认 1）

#### T10 `wafw00f_detect`（建议，有 bin 再注册）

- url: str

#### T11 `katana_crawl`（建议）

- url: str
- depth: int = 2（上限 3）
- max_urls: int = 200
- timeout_s: int = 180 → 可 job

优先收集 `.js` URL，供 jshunter 使用。输出里单独列出 `js_urls`。

#### T12 `jshunter_analyze`

目标二进制：`github.com/cc1a2b/jshunter`。Agent 必须先跑 `jshunter -h` 核对 flag，以下为意图而不是死背版本。

输入三选一（互斥）：

- `url`: 单个 JS URL（对应 `-u`）
- `urls`: list，写入 job 临时 list 文件后走 `-l`（上限 `jshunter_max_urls`）
- `file`: 仅允许 `js_workspace` 下已下载的本地 js（对应 `-f`）

开关（默认做一个「前中期」剖面，不要默认全开专业套餐）：

- `endpoints`: bool = true（`-ep`）
- `secrets`: bool = true（`-s`）
- `params`: bool = false（`-P`，中期再开）
- `jwt`: bool = false（`-x`）
- `graphql`: bool = false（`-g`）
- `firebase`: bool = false（`-F`）
- `deobfuscate`: bool = false（`-d`，重活；需要时再开，或改走 restringer）
- `threads`: int = 3（封顶配置）
- `timeout_s`: int = 180 → 超限或 urls>10 走 job

禁止：任意 `-H` 里塞换行/命令、任意 `-p` 代理字符串不校验、把 Discord/Telegram webhook 接进来（那是另一个同名项目的功能，本版不做）。

输出给模型的摘要字段：

- endpoints / urls 去重列表（上限 N）
- secrets 命中：类型 + 红acted 值（只留前后 4 字符）
- params / graphql / firebase 标志
- 原始 JSON 路径（若 `-j -o` 写到 js_workspace）

> 对已发现的 JS 做端点、参数和敏感信息抽取。web 前中期分析主工具。不替代 crawler（先 katana/httpx）。重度反混淆请用 restringer，不要默认 `-d`。

#### T13 `restringer_deobfuscate`

目标二进制：HumanSecurity/PerimeterX `restringer`（Node CLI）。

- `file`: 必须是 `js_workspace` 内已有文件，或由 MCP 先把 `source_url` 下载进 workspace 再处理
- `source_url`: 可选，scope 内 JS URL，下载后再跑
- `clean`: bool = false（`-c`，文档标 unsafe，默认关）
- `max_iterations`: int = 5（封顶 10）
- `timeout_s`: int = 60

规则：

- 输入大小 > `restringer_max_input_bytes` 拒绝
- 只写输出到 `js_workspace/<job>/xxx-deob.js`，禁止用户指定任意 `-o` 路径
- 返回：是否发生变化、输出路径、截断后的源码预览（默认前 200 行）、体积
- restringer 内部会用 isolated-vm 跑部分 unsafe 方法：必须独立超时，失败当 tool error，不要拖垮 MCP 进程
- 不要对整个 minified vendor bundle（jquery/react 等）默认跑；playbook 写明优先业务 bundle

> 对混淆/打包后的 JS 做字符串还原与逻辑简化，便于人/模型读端点与密钥上下文。输入必须是已落盘的 JS。不是通用 JS 执行器。

下载辅助（不要做成独立 MCP tool 除非基座没有 http get）：jshunter/restringer 模块内私有函数 `fetch_js(url) -> workspace path`，校验 Content-Type/大小，写 workspace。

#### T14 `nuclei_tech`

- urls: list[str]
- extra_tags: list[str] | None

固定 tags 偏向 `tech,waf,cdn` 一类；severity 允许 info,low。
禁止用户传任意 `-t` 路径。

#### T15 `nuclei_vuln`

- urls: list[str]
- tags: list[str] = ["cve","misconfig","exposure"]
- severity: list[str] = ["medium","high","critical"]
- timeout_s: int = 600 → job

模板根目录锁死官方 nuclei-templates。并发读配置上限。

#### T16 `ffuf_fuzz`

- url_pattern: str（必须含 `FUZZ`）
- mode: `dir` | `vhost` | `param`
- wordlist: str（必须落在 allowed_prefixes）
- threads: int = 10（封顶 20）
- match / filter: `fc`,`fs`,`mc` 可选
- extensions: str | None（仅 dir 模式）

拒绝：wordlist 在 /tmp、家目录任意文件、url_pattern 无 FUZZ。
大词表 → job。

Phase 3 完成标准：nuclei 不能扫到 templates 目录之外；ffuf 不能用任意路径词表；jshunter 不能打任意 header/webhook；restringer 不能写 workspace 以外的文件。

---

### Phase 4 — 编排 + job UX

#### T17 `recon_domain_passive`

输入：`domain`
流水：subfinder →（可选 amass_passive 若 available）→ dnsx
返回：hostnames、resolved ips、失败步骤。
对目标业务端口不主动连（dns 查询除外）。

#### T18 `recon_web_active`

输入：`domain` 或 `targets`，`authorize_active: bool`（必须 true 否则拒）
流水：dnsx（若是域）→ naabu top100 → httpx → nuclei_tech
可选 whatweb 对 httpx 成功 URL 的前 N 个。
长任务 job。不在这条里默认跑 nuclei_vuln 或 ffuf。

#### T19 `recon_js_analyze`

输入：`start_url` 或 `js_urls: list[str]`，`authorize_active: bool` 必须 true。

流水：

1. 若只有站点 URL：katana（有则）或从 httpx/页面里收 `.js`（实现时选基座已有的最简单办法，不要再写一个爬虫）
2. 对每个 JS（上限 20）`jshunter_analyze`（endpoints+secrets）
3. 对「明显混淆」（启发式：极高单行密度、_0x 标识、obfuscator.io 痕迹）且体积合格的文件跑 `restringer_deobfuscate`，再可选二次 jshunter `-f`

返回：js 列表、endpoints、redacted secrets、deob 文件路径。长任务 job。

这是 web 前中期分析主路径。

#### T20 `recon_job_status` / `recon_job_logs` / `recon_job_cancel`

若基座已有通用 job，只做 alias 或文档说明用通用接口，避免重复。

#### T21 `recon_health`

返回：各二进制 version / found（含 jshunter、restringer、node）、scope 是否已配置、limits、js_workspace 是否可写。不探测任何第三方 API key。

---

## 5. Description 与注册要求

- 每个 tool 的 description 放 `docs/recon-toolkit/TOOLS.md`，与代码字符串保持一致（不要两处长期分叉；能从同一常量读最好）。
- tool name 用上表蛇形，稳定，不要过两天改名。
- 参数用明确类型 + enum，少用自由字符串。
- 每个 tool docstring 写一句「Out of scope targets are rejected」。

---

## 6. 测试策略（Agent 必须写测试，不是“本地我试过”）

### 单测（无外网、可 CI）

- scope：允许/拒绝子域、CIDR、URL、host:port
- argv builder：每个 tool 至少 3 组（默认、aggressive 被拒、超限被拒）
- wordlist 路径穿越被拒
- nuclei 自定义模板路径被拒
- jshunter urls 超限被拒
- restringer 拒绝 workspace 外路径、超大文件
- 输出截断
- 缺 bin 时错误信息
- secrets 摘要必须 redact（单测用假密钥）

### Smoke（人类授权的 lab 目标，默认不要在 CI 打网）

环境变量 `RECON_SMOKE_TARGET` 才跑。
建议只跑：subfinder + dnsx + httpx 对一个人类指定域。JS 链只对同一授权域的单个 `.js` smoke。

禁止 Agent 自作主张扫公网随机目标。

---

## 7. 安全与合规（写进代码，不只写进 README）

1. 无 scope 配置时：默认拒绝所有非 loopback 目标，或启动时要求配置（二选一，推荐拒）。
2. 日志审计：tool name、redacted argv、operator 无则 `mcp`、utc time、job_id。
3. 不在错误信息里回显完整环境、家目录无关文件。
4. README 加醒目声明：仅授权测试；使用者对扫描负责。
5. SYN scan：检测 euid；非 root 自动 fallback connect，并在结果里注明。
6. JS workspace 出站下载要走 scope；落盘文件权限 0600；job 结束后可保留供人类看，但不要把完整密钥原文送回模型。
7. restringer 当不可信代码处理：超时、体积上限、独立进程，失败即杀。

---

## 8. 文档交付

Agent 写：

- `docs/recon-toolkit/BASELINE.md` — 基座调查
- `docs/recon-toolkit/TOOLS.md` — 每个 tool 的 description + 参数 + 例子
- `docs/recon-toolkit/AGENT_PLAYBOOK.md` — 建议调用顺序（被动→解析→端口→httpx→指纹→收 JS→jshunter→必要时 restringer→nuclei/ffuf）
- README 一节：scope 示例、Arch/npm/go 安装提示

JS 相关安装（文档即可）：

```
# Go 版 jshunter（本计划指定的那份）
go install -v github.com/cc1a2b/jshunter@latest

# restringer
# 需要 Node 20+
npm install -g restringer
```

Arch/BlackArch 安装提示（文档即可，不要在计划里 `sudo pacman` 乱装一堆）：

```
# 按需，不要默认全装 blackarch
pacman -S nmap whatweb ffuf amass
# PD 工具可能在 blackarch 包名略有不同，以 pacman -Ss 为准
```

---

## 9. 实施日程（按检查点，不按小时吹）

| 检查点 | 交付 | 人类动作 |
|---|---|---|
| A | BASELINE.md + bins 探测脚本 | 确认框架/权限/scope |
| B | common/scope/bins/config + 单测 | 确认无 shell=True |
| C | Phase 1 tools | 看 TOOLS.md 文案 |
| D | Phase 2 tools | 确认 naabu/nmap 默认够怂 |
| E | Phase 3 tools（含 jshunter/restringer） | 确认 nuclei/ffuf 路径锁、JS workspace、密钥 redact、jshunter 二进制认对 |
| F | workflows + health + 文档 | 用 agent 真连 MCP 走一遍被动链 + 一条 JS 分析链 |
| G | 修 smoke 暴露的解析/截断问题 | 宣布第一版冻结 |

每阶段结束：`git` 可运行状态，不要一个巨型 WIP commit 到最后。

---

## 10. 给本地 Agent 的工作方式

1. 先读基座，再写 BASELINE，停等确认 A。若人类不在，仍须把假设写进 BASELINE「ASSUMPTIONS」一节，不得静默猜错框架。
2. 每个 tool：先 `tool -h` 核对真实 flag，禁止凭记忆写 `-oJ` 还是 `-json`。
3. 改动保持最小：不重构基座无关模块。
4. 发现基座 `shell=True`：先修或包一层安全 exec，再继续。
5. 不要添加计划外 tool。要加先写进 ASSUMPTIONS 等人类。
6. 提交信息按 phase：`feat(recon): scope+exec layer` 这类。
7. 若用 Python：类型注解 + 不要在 tool 函数里写业务 200 行，builder 与 parse 分开以便测。



第二版候选已在对话里，不在本次实现范围。
