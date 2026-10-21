# Fuyu userdata

`userdata2/` 是 Fuyu profile 的可公开人格模板，由 `~/.aki/fuyu.conf` 中的
`[gateway] userdata = userdata2` 指向。

同步方式：

```bash
python3 scripts/sync-persona.py --source userdata2 --agent-id <fuyu-agent-id>
akictl fuyu set-username Master
```

`akictl fuyu set-username` 会自动使用这里的人格模板，不会改动 Aki 的 `userdata/`。
