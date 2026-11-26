# IB Gateway 安装与恢复

以下步骤由运行账户从 fertile 仓库根目录或部署中的 `current/fertile` 执行，适用于 supervisord 管理本机 paper Gateway 的实例。先按 [bootstrap](../deploy/bootstrap.md) 安装系统库并确定持久根。普通服务器的 systemd 核心服务由运维另行提供 paper Gateway，见 [部署指南](../deploy/linux-server.md)。

```sh
. "${FERTILE_ROOT:?先设置实例的持久根}/env.sh"
scripts/install-ibgateway.sh
scripts/install-ibgateway.sh
scripts/install-ibgateway.sh --verify-only
```

每次安装都打印两个归档的 SHA256 检查结果，重复运行复用同一版本的安装。离线机器先把经过批准的固定原始包放入 `cache/downloads/ibgateway/`，再运行 `scripts/install-ibgateway.sh --offline`。缓存校验失败时先隔离损坏包，再重新获取。已有同版本目录缺少安装校验标记时，停止服务、备份目录并人工核对后重新安装。

## Paper 登录

用 `install -d -m 700 "$FERTILE_ROOT/secrets"` 建立私有目录，在 `umask 077` 的会话中用本地编辑器填写 `$FERTILE_ROOT/secrets/ibkr-paper.env`。需要两行 `IBKR_USERNAME=完整用户名` 和 `IBKR_PASSWORD=完整密码`。等号右侧按字面读取，支持空格与符号，不加 shell 引号，不使用 export。运行用户必须拥有该常规文件，权限为 0600。不要把凭据写入命令参数、聊天、日志或 PR。缺少凭据时状态为 `needs_credentials`，supervisor 跳过两个进程。

```sh
scripts/fertile ensure
scripts/fertile status --json
scripts/ibgateway.sh status
scripts/ib-check.sh
```

首次登录或周期性重新认证时，账户持有人在 IBKR 的受信设备确认 paper 的 2FA；必要时通过盒子上受控的本地图形会话查看 `:87`（Xauthority 位于运行用户的 `run/ibgateway/Xauthority`）。在 Gateway API 设置中确认只允许本机、Read-Only API、4002。每日计划重启为 **23:45 UTC**。禁止启用实盘、写权限或把 X11/API 暴露到公网。

## 故障与演练

`startup` 表示已尝试启动；`ready` 表示成功完成账户和时间握手。登录窗口最多等待 150 秒，失败后按 5 秒、20 秒退避。三次未成功后进入 `needs_attention`，即使主管重启也沿用已记录预算。查 `logs/ibgateway/events.jsonl` 中的脱敏原因及 `run/ibgateway/state.json`。遇到安装、Xvfb、读权限、回环限制或账户身份问题，先解决原因。重新核准登录后使用：

```sh
scripts/ibgateway.sh reset
scripts/fertile ensure
scripts/ib-check.sh
```

守护进程会重新读取预算；若手动停止过服务，先恢复主管。状态 `orphaned_gateway_group` 时，核对运行用户的 IBC/JVM 进程组及 4002 的持有者，终止确认属于旧部署的组，勿误停其它进程。若需手动检查监听，使用 `ss -ltn '( sport = :4002 or sport = :4001 )'`；只允许 `127.0.0.1:4002`。直启调试时先停主管对应的服务，分别运行 `scripts/ibgateway.sh xvfb`、`scripts/ibgateway.sh run`，停止顺序相反；正常运行使用主管。

真实 paper 演练在账户持有人可确认 2FA 时进行。先记录 UTC 时间，确认 `ib-check.sh` 提供正的 server time 与 DU 前缀。从下面命令确认 **本次 Gateway 的 Java PID**，保留 IBC/Xvfb/主管。核对后向该 PID 发送 `kill -9`。每 3 秒执行一次 `ib-check.sh`，记录首个成功 UTC、恢复秒数和 DU 前缀，验收上限为 180 秒。超过时记录实际结果，核实 2FA、日志与重试预算。隔离环境的无凭据保护、绑定和离线安装测试不能替代此演练。

```sh
pgrep -a -u "$(id -u)" -f 'ibcalpha\.ibc\.IbcGateway'
scripts/ib-check.sh
# 确认进程后在 shell 中置入核准的 PID
date -u
start=$(date +%s)
kill -9 "$gateway_java_pid"
while ! scripts/ib-check.sh; do
  if [ "$(($(date +%s) - start))" -ge 180 ]; then echo '恢复超时'; break; fi
  sleep 3
done
echo "elapsed_seconds=$(($(date +%s) - start))"
```
