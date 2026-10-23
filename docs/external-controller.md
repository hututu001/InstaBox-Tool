# 独立控制端：续租、七小时轮换、冷却恢复

这是本 fork 新增的**显式启用功能**。上游作者为 [Pre / Pretic](https://github.com/Pretic/InstaBox-Tool)。原有 `ibt keepalive` 菜单与自动安装行为不变，不会因为更新工具就开始暂停机器。

控制器必须放在另一台常在线的 Linux 主机，例如香港 VPS，不能只放在被管理的 InstaBox 内部。一个脚本完成所有流程，systemd 约每分钟运行一轮，不在进程里睡七小时。默认每十分钟续租一小时，机器连续运行七小时后主动 suspend，确认暂停后等待至少三十秒，再 resume。恢复确认后立即续租，并以平台的新 `runStartedAt` 开始下一轮。

## 官方规则与边界

依据 [InstaBox OpenAPI](https://api.instabox.sh/v1/openapi.json) 的 `MaxRunMinutes`、`Machine`、活动租约与生命周期接口：

- 租约最长 3600 秒；到期后开始完整的闲置期。有效租约不取消连续运行上限。
- 免费机器连续运行上限默认 480 分钟。`-1` 表示无限制，`0` 表示立即睡眠，不能把 `0` 当成禁用限制。
- 达到上限后的冷却默认十分钟，实际以 `cooldownUntil` 和 `Retry-After` 为准。所有唤醒路径在冷却中都可能返回 `409 machine_cooldown`。
- `runStartedAt` 只在确认睡眠后重置。主动暂停能否如预期重置当前账号的时钟，**仍需实机验收**；回归测试模拟官方契约，不冒充实测。
- 显式生命周期操作会让已有租约失效，恢复后必须立刻重新续租。
- `suspend` 不发送 JSON 请求体。`suspend` / `resume` 都使用持久化的 `Idempotency-Key`，HTTP 202 仅表示受理。
- `cooldownUntil` 可能保留在后续运行记录中，不能仅凭字段非空就等待或恢复机器。

## 同一个循环

1. **正常运行**：读取平台状态，每十分钟 PUT 同一 lease ID，TTL 一小时；首次接管和发现新一轮运行时立即续租。成功时记录平台实际返回的 `expiresAt`。
2. **七小时到点**：先持久化轮换意图和幂等键，再请求暂停。确认 `suspended` 后开始计算三十秒等待；一分钟调度下实际可能等待约一分钟。之后请求恢复。
3. **遇到有效冷却**：优先等待平台截止时间。晚三分钟发现十分钟冷却，只等剩余七分钟，不在每轮检查中重新计算十分钟。冷却到点后尝试恢复。
4. **恢复后**：确认 `running`、`runtime.ready` 和新的 `runStartedAt`，重新建立租约。若时钟未更新，记录 `run_reset_unconfirmed`，继续保活和观察，不声称轮换成功，不反复暂停。
5. **重试**：网络错误/5xx 默认下一分钟重试，没有“三次失败永久退出”的上限。401/403 等配置或权限错误降低为十分钟一次，方便修正凭据后自行恢复。429/冷却遵守 `Retry-After`。连续失败通过健康检查报告，不能把持续重试当作成功。

冷却结束后的 resume 可以连续失败多次，后续仍继续尝试。若恢复又返回 `machine_cooldown`，优先按 `Retry-After` 延后；缺少提示时保守等待十分钟，再继续尝试，并记录原因。

请求超时不等于操作失败：下一轮先读取机器状态。已经暂停就不再暂停；已经恢复就不再恢复。拿到 operation ID 后查询操作；没有拿到则复用原幂等键确认。明确失败的操作才换新幂等键重试。操作声称成功但机器仍不符合目标时，给两分钟状态收敛时间，再记录不一致并重试。超过七天幂等窗口的未知请求保留现场并报告，不盲目重发。

控制器不会 start 已停止机器，也不因历史冷却而恢复后来手动暂停的机器。对自己没有发起、也没有有效冷却证据的暂停，不直接调用 resume；若已维护的租约过期，可以尝试官方租约接口恢复闲置暂停，平台拒绝后不会绕到 resume。

## 安装到独立 Linux 控制端

只需要 Python 3.10+ 和 systemd，不需要 Node、Tailscale 或目标机器 SSH。以下是安装说明，不会在复制仓库时自动启用服务。

```bash
git clone https://github.com/hututu001/InstaBox-Tool.git
cd InstaBox-Tool
mkdir -p "$HOME/.local/lib/instabox-controller" "$HOME/.config/instabox-controller" \
         "$HOME/.local/state/instabox-controller" "$HOME/.config/systemd/user"
chmod 700 "$HOME/.config/instabox-controller" "$HOME/.local/state/instabox-controller"
install -m 700 external_controller.py "$HOME/.local/lib/instabox-controller/external_controller.py"
install -m 600 examples/external-controller.example.json "$HOME/.config/instabox-controller/config.json"
```

编辑 `~/.config/instabox-controller/config.json`，将占位机器 ID 换成实际目标。若有多个控制端，使用各自的 lease ID；同一机器只保留一个生命周期管理者。租约至少覆盖三个续租间隔，默认覆盖六个。七小时阈值遇到更短的正数平台上限时，会提前到上限前十分钟。

交互写入具有 `machines:write` 权限的密钥，不要把密钥作为命令行参数或写进仓库：

```bash
read -rsp 'InstaBox API Key: ' controller_key; printf '\n'
(umask 077; printf '%s\n' "$controller_key" > "$HOME/.config/instabox-controller/api-key")
unset controller_key
```

**启用前**：停止维护同一机器的旧自动任务，避免多个生命周期控制器相互干扰。香港已有自定义 system timer 时，在实际迁移验收中单独停旧、启新；此文档不自动修改它，也不删除旧记录。

```bash
install -m 600 examples/instabox-external-controller.service "$HOME/.config/systemd/user/"
install -m 600 examples/instabox-external-controller.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now instabox-external-controller.timer
sudo loginctl enable-linger "$USER"
```

定时器在开机后首次运行，之后约每分钟推进一轮；服务失败后重试，状态文件让重启可续接。多目标逐个处理，每轮执行时间会使实际间隔变长，默认一台目标最便于验收。

停用：`systemctl --user disable --now instabox-external-controller.timer`，若服务仍执行，再停止同名 `.service`。停用不关目标机器、不删除状态、不撤销 API Key；租约自然到期。

## 日志、健康检查、飞书对接

- `~/.local/state/instabox-controller/state.json`：原子持久化状态，每次生命周期提交前落盘。不要在进行中手工清空。
- 同目录 `controller.jsonl`：JSON 行日志，UTC 时间，每轮有 cycle ID；记录续租到期、冷却、请求、重试、状态确认、时钟重置、卡住的操作。按 UTC 日轮转，保留最近三十个轮转文件；无 API key、无原始接口错误正文。
- systemd journal 同时保留事件：`journalctl --user -u instabox-external-controller.service`。

只读检查命令，不访问目标机器：

```bash
python3 "$HOME/.local/lib/instabox-controller/external_controller.py" \
  --config "$HOME/.config/instabox-controller/config.json" \
  --key-file "$HOME/.config/instabox-controller/api-key" \
  --state "$HOME/.local/state/instabox-controller/state.json" \
  --log-file "$HOME/.local/state/instabox-controller/controller.jsonl" --health
```

`--status` 输出本地状态；`--health` 输出 JSON，正常退出 0，异常退出 1。健康检查能识别控制器三分钟未更新、连续接口失败、正常运行时二十分钟未续租、轮换/恢复超时、时钟重置未确认等。正常冷却等待不会仅因缺少续租被误报。

现有飞书告警应每两分钟调用健康检查，只在故障/恢复变化时通知；**本模块不保存飞书 webhook，也不会自动改写现有告警脚本**。上线时须一并适配原有只看续租时间的告警条件。单台控制端整体断网的告警需要控制端之外的观察者，systemd 自身不是第二台机器。

## 回归与部署是两件事

```bash
python3 -m unittest discover -s tests -p 'test_external_controller.py' -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

测试使用假时钟、假 API，不真的等待七小时、不暂停机器，不读取真实密钥。Windows 能跑新控制器测试；原项目的 fcntl、PTY、bash、systemd 等回归应在 Linux 跑。可把源码发送到 InstaBox 临时目录执行，再取回报告，无需部署测试源码到线上服务。所有回归通过也不能替代主动暂停的实机验收。

## 异常恢复与轮换调度更新

正常状态仍由一分钟 timer 驱动，十分钟续租一小时。进入本控制器的暂停/恢复流程后，单次执行最多用三分钟、每十秒推进一次；完成、接口失败或等待明确冷却期限时退出，交回 timer。暂停确认后仍等待至少三十秒。实际停机时间取决于平台操作速度，不能保证固定秒数。

`machine_cooldown` 未携带 `Retry-After` 时，一分钟后重新读取机器状态再尝试；不额外推测一个不可缩短的十分钟冷却。存在明确 `cooldownUntil` 或 `Retry-After` 时仍遵守期限。重试不会因次数达到固定上限而停止。

无当前恢复任务的 `unmanaged_pause` 会报告不健康，但不会直接调用 resume。正常冷却只抑制预期的租约和流程耗时告警，不再掩盖连续接口失败。机器 running 但 runtime 尚未 ready 时，在没有暂停意图或进行中生命周期操作的情况下维持租约；恢复成功仍需确认 ready 和新的运行起点。

香港飞书适配会在首次故障、新增故障、全部恢复时通知，相同故障不会重复发送。香港服务 TimeoutStartSec 使用五分钟，以容纳上述短时快速检查。
