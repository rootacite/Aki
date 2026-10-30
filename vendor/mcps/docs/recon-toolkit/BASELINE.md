# Recon Toolkit — 基座调查（检查点 A）

调查日期：2026-08-28  
仓库：`/home/acite/Deeppin/mcps`  
主机：Linux 7.1.9-zen1-2-zen x86_64（Arch + BlackArch 包可见）  
用户：`uid=1000(acite)` groups=`acite,nobody`

探测脚本：`scripts/check_recon_bins.sh`（`bash scripts/check_recon_bins.sh`）。

## 检查点 A — 已确认（2026-08-28）

人类确认：

| # | 议题 | 决定 |
|---|---|---|
| 1 | 框架 | FastMCP 3.4.7 + uv + Python 3.14；stdio；`@mcp.tool()` 挂到现有 `mcp` |
| 2 | SYN / 提权 | **默认允许提权跑 `-sS`**（`privilege.allow_syn_scan` + `allow_sudo`）。无 raw 权限时记录并按 `fallback_connect` 降级，不静默 |
| 3 | scope 文件 | `config/recon.yaml`（可用 `MCP_RECON_CONFIG` 覆盖） |
| 4 | restringer | 默认可跑；`isolated-vm` + 1MiB + max-iterations + sb-timeout；禁止任意 `-o` |
| 5 | jshunter | `/usr/local/bin/jshunter` = cc1a2b v0.7.6；flag 以本机 `-h` 为准（`-ep/--end-point`） |
| 6 | httpx | 包装 `httpx-pd`，禁止 Python `/usr/bin/httpx` |
| 7 | 缺的包 | 人类已装 **dnsx, katana, wafw00f, gau, waybackurls**。theHarvester 仍缺 |
| 8 | exec 层 | 先写安全 `run_recon`（检查点 B），再挂业务 CLI |

复跑探测（安装后）：必装 10/10 FOUND（httpx 名仍撞 python-httpx，PD 在 `httpx-pd`）。可选缺 theHarvester。katana 为 `/usr/bin/katana`（`katana-bin 1.7.0-1`），不是 `katana-pd`。gau 为 `/usr/bin/gau` 2.2.4（真实二进制）。

未扫任何目标。

---

## 1. MCP 框架

| 项 | 事实 |
|---|---|
| 框架 | **FastMCP 3.4.7**（`from fastmcp import FastMCP`），不是官方 MCP Python SDK 直连，不是自研协议栈 |
| OpenAI 客户端 | `openai 3.3.1`（仅现有 local-AI tools 调本地模型用） |
| 语言 | Python **3.14**（`.python-version` = `3.14`，`requires-python >= 3.14`） |
| 包管理 | **uv** 0.12.6；build backend `uv_build`；锁文件 `uv.lock` |
| 包名 / 入口 | 发行名 `mcps`；console script `mcps = "mcps:main"` |
| 入口文件 | `src/mcps/__init__.py` → `server.main`；`src/mcps/__main__.py`（`python -m mcps`） |
| Server 对象 | `src/mcps/server.py` 模块级 `mcp = FastMCP("local-ai-mcp", instructions=...)` |
| 默认 transport | **stdio** |
| 可选 transport | `--transport sse \| streamable-http`，默认 `--host 127.0.0.1 --port 8000` |

运行方式（README）：

```
uv run mcps                          # stdio
.venv/bin/mcps                       # 绕过 uv cache
uv run mcps --transport sse --host 127.0.0.1 --port 8000
```

现有 7 个 LLM 预处理 tool（与 recon 无关，不要改）：`summarize` `classify` `extract` `batch_analyze` `code_review_precheck` `compress_context` `ask`。

---

## 2. Tool 注册方式

**装饰器，不是 registry / yaml。**

全部写在 `src/mcps/server.py`：

```python
mcp = FastMCP("local-ai-mcp", ...)

@mcp.tool()
async def summarize(...) -> str:
    ...
```

- 无独立 `tools/` 包，无动态发现。
- 无 MCP resource / prompt 注册。
- 函数签名即 schema；返回 `str`（多数是 JSON 字符串）。
- 校验失败用 `raise ValueError(...)`，FastMCP 会变成 tool error。

**对 recon 的映射（建议，等确认 A 后再动代码）：**

- 保持同一个 `mcp` 对象，不要第二个 FastMCP 进程。
- 按计划在 `src/mcps/tools/recon/` 分模块写函数，在 `server.py` **import 它们**（或 `from .tools.recon import register; register(mcp)`），继续用 `@mcp.tool()`。
- 不要把 recon 塞进现有 7 个 LLM tool 所在文件的业务逻辑里。

---

## 3. 进程执行 / 日志 / job

| 能力 | 现状 |
|---|---|
| `run_cmd` / 安全 exec | **没有**。`src/` 内零处 `subprocess` / `Popen` / `os.system` / `shell=True` |
| timeout 杀进程组 | 没有 |
| cwd / env 白名单 | 没有 |
| 输出截断 | 没有（现有 tool 只读本地文件 + 调 LLM） |
| 日志 | 项目内无 `logging` 封装；依赖 FastMCP/stdio 默认行为 |
| job 系统 | **没有**（无 job_id、无后台任务目录） |
| 密钥 redact | 没有（现有密钥只是 `LOCAL_AI_API_KEY`，默认 `sk-local`） |

计划硬性约束：禁止再造第二套 subprocess。基座目前 **连第一套都没有**。

**结论（待确认 A 之后、挂 CLI 之前）：必须先补一层安全 exec**（list argv、禁 `shell=True`、timeout 杀进程组、cwd/env 控制、stdout/stderr 截断、argv/env redact）。长任务 job 也要新建（基座无现成可适配的）。这是检查点 B 的工作，本文件不实现。

---

## 4. 配置 / 密钥加载

现有配置 **只有环境变量**，集中在 `src/mcps/llm.py`：

| 变量 | 默认 |
|---|---|
| `LOCAL_AI_BASE_URL` | `http://127.0.0.1:10001/v1` |
| `LOCAL_AI_MODEL` | `/opt/models/language/Qwen3.8-9B-Q8_0.gguf` |
| `LOCAL_AI_API_KEY` | `sk-local` |
| `LOCAL_AI_MAX_TOKENS` | `1024` |
| `LOCAL_AI_TIMEOUT` | `300` |
| `LOCAL_AI_CONCURRENCY` | `4` |
| `LOCAL_AI_ENABLE_THINKING` | `0` |
| `LOCAL_AI_MAX_RETRIES` | `2` |

- 无 `config/*.yaml`、无 dotenv 加载器、无 XDG 配置模块。
- 仓库里 **还没有** `config/recon.yaml`。
- 读文件：`src/mcps/utils.py` 的 `read_file_text` / `read_items`（`~` 展开，相对 server cwd）。**无路径白名单**（recon 的 wordlist / js_workspace 不能直接复用这个函数当任意路径读）。

密钥策略（计划）：BBOT / amass 走各自官方 config；本版不集成 Shodan；不把 API key 放进 tool 参数。

---

## 5. 测试怎么跑

| 项 | 现状 |
|---|---|
| `tests/` | **不存在** |
| pytest | 未进 `pyproject.toml` 依赖 |
| 现有测试 | `uv run python scripts/smoke_test.py` — 对 **活的本地模型** 打 7 个 tool |
|  | `uv run python scripts/path_test.py` — 文件 `path` 输入的 smoke |
| CI | 无 |

Recon 单测（argv builder / scope / truncate / unavailable bin）需要 **新建** `tests/recon/`，并加 pytest（或 unittest）。不要绑 smoke 到公网目标。

---

## 6. 源码地图（基座）

```
src/mcps/
  __init__.py      # main 再导出
  __main__.py      # python -m mcps
  server.py        # FastMCP + 7 个 @mcp.tool + argparse
  llm.py           # AsyncOpenAI + env
  prompts.py       # 本地模型 system prompts
  utils.py         # 读文件 / JSON 容错 / chunk
scripts/
  smoke_test.py
  path_test.py
  check_recon_bins.sh   # 本次新增
docs/recon-toolkit/
  BASELINE.md           # 本文件
```

---

## 7. 本机二进制（`scripts/check_recon_bins.sh`）

跑于 2026-08-28T06:35:38+08:00。Node v26.8.1（满足 restringer 的 Node 20+）。

### 7.1 必装

| 二进制 | PATH | CLI 自报 | pacman | 备注 |
|---|---|---|---|---|
| subfinder | `/usr/bin/subfinder` | `-version` **失败**：缺 `~/.config/subfinder/config.yaml` | `subfinder 1:v2.14.0.r44.g51e9b906-1` | PD 工具。help 确认 JSON 为 `-oJ`/`-json`，有 `-silent` |
| dnsx | `/usr/bin/dnsx` | Current Version **1.3.1** | `dnsx 1:v1.2.1.r465.g3ecbcb1-1` | 人类确认 A 后已装 |
| naabu | `/usr/bin/naabu` | Current Version **2.6.1** | `naabu 1:v2.3.4.r370.ged04418-1` | git snapshot，CLI > 包名 epoch |
| httpx | `/usr/bin/httpx` | `Usage: httpx [OPTIONS] URL` | **`python-httpx 0.28.1-7`** | **错的二进制**（encode/httpx） |
| httpx-pd | `/usr/bin/httpx-pd` | Current Version **v1.10.0** | `httpx 1:v1.7.4.r222.g50b901a-1` | **真正的 projectdiscovery/httpx** |
| nuclei | `/usr/bin/nuclei` | Engine **v3.11.1** | `nuclei 2:v3.6.2.r415.gbb79d8e-1` | 启动有 sonic/ast fallback 警告 |
| nmap | `/usr/bin/nmap` | **7.991** | `nmap 7.991-1` | 无 cap、无 setuid |
| whatweb | `/usr/bin/whatweb` | **0.6.4** | `whatweb 1:v0.6.4.r0.gd279d93-1` | |
| ffuf | `/usr/bin/ffuf` | `git-20260820-33c67d28` | `ffuf 1:v2.2.1.r20.g33c67d2-1` | |
| jshunter | `/usr/local/bin/jshunter` | banner **v0.7.6** | 非 pacman | **MATCH `github.com/cc1a2b/jshunter`**（见 §7.3） |
| restringer | `/usr/bin/restringer` | **2.2.0** | 非 pacman；`npm ls -g` → `restringer@2.2.0` | symlink → `/usr/lib/node_modules/restringer/bin/deobfuscate.js` |

必装缺失：无（复跑后 10/10 在 PATH 上；httpx 名仍撞车）。  
必装名存在但不可直接当 recon 用：**`httpx` = Python 客户端**。实现时 BIN 必须指向 `httpx-pd`（或 identity 校验）。

### 7.2 建议有

| 二进制 | PATH | CLI / pacman | 备注 |
|---|---|---|---|
| amass | `/usr/bin/amass` | CLI **v5.1.1** / `amass 3:v5.1.1.r0.g79299dc-1` | `amass -version` 会尝试 mkdir `~/.config/amass`，本环境 **permission denied** |
| bbot | `/usr/bin/bbot` | 包 `bbot 3:v3.0.1.r0.geabac72-1` | **跑不起来**：`PermissionError` on `multiprocessing.SemLock`。本环境可能有 no-new-privileges / 沙箱限制 |
| katana | `/usr/bin/katana` | `-version` 因缺 flags 文件失败 | `katana-bin 1.7.0-1` | 人类已装。**不是** `katana-pd` |
| wafw00f | `/usr/bin/wafw00f` | help 可用 | `wafw00f 902.1fb92e6-1` | 人类已装 |
| masscan | `/usr/bin/masscan` | **1.3.2** / `masscan 1.3.2-2` | 无 cap；SYN 同样要特权 |
| theHarvester | **MISSING** | 仓里包名 `theharvester` | 未装 |
| gau | `/usr/bin/gau` | **2.2.4** / `gau 171.8201b9d-1` | 真实二进制。shell alias 仍须忽略 |
| waybackurls | `/usr/bin/waybackurls` | `/usr/bin/waybackurls` | `waybackurls 11.89da10c-1` | 人类已装 |

### 7.3 jshunter 身份（检查点 A 必答题）

路径：`/usr/local/bin/jshunter`（`go install` 风格，非 pacman）。

`jshunter -h` 横幅：`v0.7.6 (latest)  Created by cc1a2b`。

已见 flag：

- `-u` / `--url`
- `-l` / `--list`
- `-f` / `--file`
- `-s` / `--secrets`（不是计划字面 `--secrets` 以外的同名 mass-scanner）
- `-ep` / `--end-point`（计划写的是 `--endpoints`；**本机实际是 `--end-point`**）
- 另有 `--har`、`-d/--deobfuscate`、`-t/--threads`、`--allow-internal` 等

**判定：这就是 github.com/cc1a2b/jshunter，不是 PyPI 同名 mass-scanner。可以包。** argv 以本机 `-h` 为准，不要写错 `--endpoints`。

### 7.4 restringer（检查点 A：unsafe / isolated-vm）

CLI 2.2.0 相关选项（摘自 `-h`，未执行反混淆）：

- `--sandbox <name>`：`isolated-vm` / `node` / `deno` / `bun`（默认 current runtime）
- `--sb-timeout <ms>` 默认 1000
- `--sb-memory-limit <mb>` 默认 128（isolated-vm）
- `-m, --max-iterations`
- `-c, --clean` 标注 **unsafe**
- **没有**文件大小 flag → 必须在 MCP 包装层强制 `restringer_max_input_bytes`（计划默认 1MiB）

默认建议（等人类点头）：允许跑；sandbox=`isolated-vm`；强制 size + iterations + `--sb-timeout`；禁止调用方指定任意 `-o` 路径（只写 `js_workspace`）。

### 7.5 SYN scan / 提权（检查点 A 必答题）

对 `nmap` / `naabu` / `masscan` 只做了 `stat` + `getcap`，**没有发包**：

- mode `755`，无 setuid
- `getcap` 为空（无 `cap_net_raw`）
- 本环境 `sudo -n` 失败：`sudo.conf` uid 异常 + **no-new-privileges**（容器/沙箱常见）

因此：**当前 MCP 进程不能做真正的 SYN scan（naabu/nmap `-sS`）**。能做的是 connect scan（`-sT` / naabu 非 raw）。是否允许以后给 MCP 进程提权（setcap /  sudoers / systemd AmbientCapabilities）—— **必须人类决定，默认按不允许写进假设。**

---

## 8. 检查点 A — 已关闭

见文首确认表。后续检查点 B：公共层 + 单测，**仍不挂业务 CLI**。

---

## 9. ASSUMPTIONS（人类不在时不得静默猜错）

写进实现前必须当「未批准」：

- 不新开 MCP server 进程；recon 与 local-ai tools 共存于 `mcps`。
- 不重构 `llm.py` / 现有 7 个 tool。
- 基座无 exec → 检查点 B 新增 `src/mcps/tools/recon/common.py` 的 `run_recon`，这是 **唯一** subprocess 入口。
- 缺 bin 不阻止 `mcp.run()`。
- `httpx` PATH 名不可信；固定探测 `httpx-pd` 或 identity。
- `gau` 必须是可执行文件，忽略 alias。
- `katana` 本机是 `/usr/bin/katana`（`katana-bin`），bins 同时认 `katana` 与 `katana-pd`。
- SYN scan **默认允许提权 `-sS`**（人类确认）；无 cap/root 时 `sudo -n` 仅针对 nmap/naabu/masscan 白名单，失败则 fallback connect 并注明。
- `allowed_domains` 含 `localhost`、`git.acitelight.top`；`allowed_cidrs` 含 `192.168.0.0/16` 与 `127.0.0.1/32`。其它私网仍拒。
- 不把 sqlmap/hydra/msf/任意 shell/`extra_args: str` 加进来。
- 本环境 `bbot` SemLock 失败、`amass` 不能写 `~/.config`：包装时要能把错误回给模型，不要当「已可用」。
- 本调查过程中的 `getcap`/`sudo` 结果可能受当前沙箱影响；人类在真实登录 shell 里若已 setcap，请在确认 A 时覆盖第 7.5 节。

---

## 10. 明确没做的事（业务 CLI）

- **没有** `tools_subfinder.py` 等业务 MCP tool，`register()` 是空钩子
- 没有改 `src/mcps/server.py` / 现有 7 个 LLM tool
- 没有对任何主机发包

---

## 11. 检查点 B — 请人类确认（公共层）

已落地：

| 路径 | 作用 |
|---|---|
| `config/recon.yaml` | scope 空（全拒）、`privilege.allow_syn_scan/allow_sudo: true` |
| `src/mcps/tools/recon/common.py` | **唯一** `run_recon`：list argv、`shell=False`、timeout 杀进程组、输出截断、env 白名单、sudo 仅 nmap/naabu/masscan |
| `scope.py` | hostname/URL/IP/CIDR/`host:port` |
| `bins.py` | `httpx` → `httpx-pd`；jshunter 认 cc1a2b |
| `jobs.py` | 后台 job + cancel 杀进程组（尚未注册 MCP tool） |
| `tests/recon/` | 29 passed |

请确认：

1. 无 `shell=True` / 无任意 argv0 可接受。
2. SYN：无 cap 时对 nmap/naabu/masscan 走 `sudo -n --`；其它 bin 绝不 sudo。无 sudo/cap 时 tool 层再 fallback connect（下一阶段）。
3. 空 scope 保持全拒绝，直到你填 lab 域/CIDR。
4. 可以开始 Phase 1 CLI（subfinder/dnsx/amass）。

未确认前不挂业务 tool。
