# Fuyu userdata

`userdata2/` 是 Fuyu profile 的可公开人格模板，由 `~/.aki/fuyu.conf` 中的
`[gateway] userdata = userdata2` 指向。Fuyu 是四季工坊的 DevOps 与质检员，
负责代码、Code Review、系统运维和 Git 巡检；他的毒舌、嘴硬和逻辑优先保持原样，
这里只补充对外的职责定位。

首次启动时这里会创建 `identity.json`，保存 Fuyu 自己的不可变 `memberId`、当前 Name、
永久别名和 `kind=agent`。它是该 profile 的权威身份清单；`[channel] id/name/aliases`
只是镜像。该文件不写入 MemFS，目录移动时身份随之移动。两个 profile 不能共用同一个
`identity.json`，否则启动会明确报错。

同步方式：

```bash
python3 scripts/sync-persona.py --source userdata2 --agent-id <fuyu-agent-id>
akictl fuyu set-username Master
```

`akictl fuyu set-username` 会自动使用这里的人格模板，不会改动 Aki 的 `userdata/`。
