# Background service lifecycle / 后台服务生命周期

AgentCaptain has two independent local services: the Python web/MCP service and the Node terminal broker. Browser closure and web-service restarts do not intentionally end a worker's terminal. Remote workers run in their original tmux sessions; reconnecting a display is distinct from starting or resuming Codex.

## Windows background launch

Use `python scripts/captain.py service start` or `run_agentcaptain.cmd`. Foreground development with `python scripts/captain.py serve` remains attached to its launching console. After an editable install, the equivalent commands are `agent-captain service start` and `agent-captain serve`.

The background path uses Windows WMI only as a hidden, current-user process factory. An external monitor launches the actual service with independent standard streams, verifies its creation identity and executable, publishes an exclusive receipt, and records its eventual exit. It does not require another account, elevation, a scheduled task, or stored credentials. It does not automatically restart services, models, tunnels, or worker connections.

`CREATE_NO_WINDOW` alone only hides a console. It does not protect a child from its parent's `KILL_ON_JOB_CLOSE` Job Object. The launcher checks its external route and fails closed instead of silently using the old launch path. Some environments place WMI-created processes in a non-killing Job: this is recorded truthfully, not described as completely job-free.

Private artifacts under `.runtime/background-launches/launch-<id>/` include:

- `plan.json`: one-shot, short-lived, private-ACL launch settings; removed when consumed or expired. Only explicitly allowed environment settings cross the launch boundary, not the full tool environment or arbitrary API credentials.
- `claimed`: exclusive claim preventing two monitors from executing one plan.
- `receipt.json`: actual service PID, exact creation identity, monitor PID, and the observed Job checks. The monitor's PID is not presented as the service PID.
- `lifecycle.jsonl`: bounded metadata for start, verification, launch failure, and actual child exit, without argv, environment contents, or authentication descriptors.

The environment filter retains standard Windows platform paths, including `PROGRAMDATA`, which native OpenSSH requires even for local configuration inspection. Removing it can make SSH exit before networking, with no stderr; this must not be mistaken for a remote-machine outage.

The process's existence and identity are verified separately from application readiness. Web readiness must match the actual launched PID. A timeout after dispatch means **unknown outcome**, not proof that nothing started: inspect the original private receipt and lifecycle before retrying. A broker recovery with unknown outcome retains its maintenance fence.

## Safe owner recovery

New broker descriptors and leases store PID plus process birth identity and executable/script identity. A reused PID must not make an unrelated live program the broker owner, and recovery never kills that unrelated process.

For legacy descriptors without a birth identity, a live non-Node executable can prove the original Node owner is gone. A live Node process, unreadable identity, ambiguous port state, or changed reference remains blocked. Recovery still requires the original descriptor/reference/lease snapshots, a definitely refused old port, and a guarded single-owner handoff. Do not delete locks or edit descriptors to bypass this check.

Recovering a broker starts an empty local display service. It does not automatically reconnect workers. Use each original worker's **Reconnect** action; the Hub verifies the exact remote tmux/pane identity, preserves stable request keys, and does not create a replacement shell, resume Codex, or replay tasks. Missing tmux and upstream network failures remain explicit.

## Diagnostics and acceptance

```sh
python scripts/captain.py service status
python -m unittest tests.test_background_process tests.test_terminal_recovery -v
```

The opt-in Windows test below creates only isolated, bounded test processes. It does not touch production Hub/broker services, SSH, tmux, or models:

```powershell
$env:RUN_WINDOWS_BACKGROUND_LIVE = '1'
python -m unittest tests.test_background_job_survival -v
```

It closes a private nested killing Job containing a temporary launch parent and a direct control child. The actual background child and monitor must retain their birth identities and continue producing a heartbeat. A test-only stop file then causes a voluntary exit, which must be observed in lifecycle logs. This tests independence from the launching host, not unattended overnight stability, OS boot startup, network resilience, or automatic crash recovery.

## 中文使用提示

后台恢复和远端模型恢复是两回事。网页打不开时，先执行 `python scripts/captain.py service status`；服务未运行才执行 `python scripts/captain.py service start`。后台恢复后，使用原工作会话的「重连」接回显示，不需要另建会话，也不应盲目重发任务。

若显示「结果未知」「原会话不存在」或身份核验失败，保留原请求与日志再核对，不要强删锁、杀同 PID 的陌生程序、另建替代 shell，或反复换请求键。Windows 关机/休眠、VPN/Tailscale/SSH 路由故障仍需独立处理。本实现没有安装开机自启、系统服务或自动唤醒模型。
