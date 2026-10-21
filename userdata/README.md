# userdata

这里保存 Aki 的可公开、非隐私人格模板与记忆策略。它是 MemFS 的源文件，
使用 `{{USERNAME}}` 占位符表示对用户的称呼。

## 包含

- `persona.md` — 核心人格模板
- `memory-policy.md` — 长期记忆写入策略模板
- `character/MEMORY.md` — 人格扩展索引
- `character/voice.md` — 语气与表达模板
- `character/relationship.md` — 与用户的关系定位模板
- `character/boundaries.md` — 身份与安全边界
- `character/examples.md` — 原创语气示例模板
- `username.txt` — 旧版默认 profile 的称呼迁移源（新状态保存在 `<profile>.conf` 的 `[gateway] username`）
- `MANIFEST.json` — 同步来源、MemFS commit 和模板哈希

## 占位符

`{{USERNAME}}` 会在同步时替换成当前称呼。`akictl set-username` 把称呼写入
`<profile>.conf` 的 `[gateway] username`；手动运行同步脚本时仍可读取本目录的
`username.txt`。例如：

```text
对{{USERNAME}}，我会认真听。
```

当 `username.txt` 是 `Master` 时，MemFS 中渲染为：

```text
对Master，我会认真听。
```

## 明确不包含

- `human.md` — 用户个人档案
- `profile/**` — 用户身份、偏好、项目与私密信息
- `MEMORY.md` — 含用户身份信息的 MemFS 根索引
- 聊天记录、transcript、凭据、API key、密码、令牌
- `.git/**` 和任何运行时数据

请不要把隐私内容放进这个目录。同步脚本只接受固定的白名单文件，
其他文件会被忽略；`human.md` 和 `profile/**` 会被明确警告并跳过。

## 修改称呼

```bash
akictl username                 # 查看当前称呼
akictl set-username Master      # 修改、渲染并重编译当前会话
```

`akictl set-username` 会把称呼写入当前 profile 的 `[gateway] username`，并只重编译
该 agent 的会话。`username.txt` 仅用于旧版默认 profile 的迁移来源。

## 同步回 MemFS

```bash
python3 scripts/sync-persona.py --dry-run   # 只显示会更新哪些文件
python3 scripts/sync-persona.py             # 渲染、备份、写入并提交 MemFS
akictl recompile                            # 手动同步后重编译当前会话
```

脚本行为：

- 读取 `--username` 覆盖值或 `username.txt`，把模板中的 `{{USERNAME}}` 替换成当前称呼
- 校验 frontmatter 和文件编码
- 把将被覆盖的旧文件备份到 `~/.letta/backups/userdata-sync-<timestamp>/`
- 只写入白名单文件
- 默认提交 MemFS git；`akictl set-username` 会自动重编译当前会话 prompt
- `--no-commit` 只写文件，不提交，system prompt 可能不会重新编译
- `--agent-id` / `--memory-dir` 可覆盖自动检测结果
