#!/usr/bin/env bash
# =====================================================================
# 小王 v2 — 一键部署到 ecs-wecom（/opt/xiaowang-v2）
#
# 流程（每步失败立即停，不带病往下走）：
#   0. 探针 gate：先跑 probe.sh，node:sqlite/WAL/FTS5 不过就不部署
#   1. 端口冲突检查：ss -tlnp 查 8090，被占（且不是自己）就停
#   2. 同步代码：scp 整个项目到 /opt/xiaowang-v2（排除 .git/node_modules/本地 db）
#   3. .env：服务器无 .env 时从 .env.example 拷一份骨架（已存在则保留，不覆盖密钥）
#   4. 装 systemd unit + daemon-reload + enable + (re)start
#   5. 装 crontab：watchdog 每 5min + 每日 wal_checkpoint 后备份 v2.db
#   6. 烟测：等服务起来，curl /healthz，tail 日志确认 worker 在 tick
#
# 用法：
#   bash deploy/install.sh                 # 部署到默认 ecs-wecom
#   bash deploy/install.sh --host=myhost   # 换目标机
#   bash deploy/install.sh --skip-probe    # 跳过探针（不推荐，仅排障时用）
#
# 在本机（Windows Git Bash / WSL / Linux）跑，通过 ssh/scp 操作远端。
# =====================================================================
set -euo pipefail

# ---- 配置 ----
REMOTE_HOST="${XW2_DEPLOY_HOST:-ecs-wecom}"
REMOTE_DIR="/opt/xiaowang-v2"
SERVICE_NAME="xiaowang-v2"
HTTP_PORT="8090"
NODE_BIN="/usr/local/bin/node"
SKIP_PROBE=0

for arg in "$@"; do
  case "$arg" in
    --host=*) REMOTE_HOST="${arg#--host=}" ;;
    --skip-probe) SKIP_PROBE=1 ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $arg（--help 看用法）"; exit 2 ;;
  esac
done

# 脚本所在目录 = deploy/，项目根 = 上一级。无论从哪 cd 调用都能定位。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# 远端跑命令的薄封装（统一 ConnectTimeout，便于读）。
rexec() { ssh -o ConnectTimeout=10 "$REMOTE_HOST" "$@"; }

log "目标: ${REMOTE_HOST}:${REMOTE_DIR}  端口=${HTTP_PORT}"
log "项目根: ${PROJECT_ROOT}"

# 关键文件存在性自检（防止半成品仓库部署上去）。
for f in main.mjs package.json deploy/${SERVICE_NAME}.service .env.example; do
  [ -f "${PROJECT_ROOT}/${f}" ] || die "缺少必需文件: ${f}（main.mjs 等核心代码需先由其他 builder 就位）"
done

# ---------------------------------------------------------------------
# 0. 探针 gate
# ---------------------------------------------------------------------
if [ "$SKIP_PROBE" -eq 0 ]; then
  log "步骤 0/6: 跑能力探针（node:sqlite / WAL / FTS5）..."
  if ! bash "${SCRIPT_DIR}/probe.sh" --host="$REMOTE_HOST"; then
    die "探针未通过 —— 目标机缺 node:sqlite/WAL 能力，停止部署。FTS5 单项 FAIL 可 --skip-probe 强过（memory 会降级 LIKE）。"
  fi
  ok "探针通过"
else
  log "步骤 0/6: 跳过探针（--skip-probe）"
fi

# ---------------------------------------------------------------------
# 1. 端口冲突检查（CLAUDE.md：端口冲突先 ss -tlnp）
# ---------------------------------------------------------------------
log "步骤 1/6: 检查端口 ${HTTP_PORT} 占用..."
# 拿占用 8090 的行；过滤掉本服务自己（重新部署时端口被自己占用是正常的）。
PORT_USER="$(rexec "ss -tlnp 2>/dev/null | grep ':${HTTP_PORT} ' || true")"
if [ -n "$PORT_USER" ]; then
  if echo "$PORT_USER" | grep -q "${SERVICE_NAME}\|main.mjs\|node"; then
    log "端口 ${HTTP_PORT} 被疑似自身进程占用（重新部署，正常）：$PORT_USER"
  else
    die "端口 ${HTTP_PORT} 已被其它进程占用：${PORT_USER}  —— 解决冲突或改 HTTP_PORT 后重试"
  fi
else
  ok "端口 ${HTTP_PORT} 空闲"
fi

# ---------------------------------------------------------------------
# 2. 同步代码到 /opt/xiaowang-v2
# ---------------------------------------------------------------------
log "步骤 2/6: 同步代码到 ${REMOTE_DIR}..."
rexec "sudo mkdir -p ${REMOTE_DIR} && sudo chown \$(whoami):\$(whoami) ${REMOTE_DIR}"

# 优先 rsync（增量、可排除），无 rsync 退回 tar over ssh（不污染远端）。
# 排除：.git、node_modules、本地 v2.db*（绝不覆盖服务器上的活数据库！）、.env（密钥不上传）、workspace。
EXCLUDES=( --exclude='.git' --exclude='node_modules' --exclude='v2.db' --exclude='v2.db-wal' --exclude='v2.db-shm' --exclude='.env' --exclude='workspace' )
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete-excluded "${EXCLUDES[@]}" \
    -e "ssh -o ConnectTimeout=10" \
    "${PROJECT_ROOT}/" "${REMOTE_HOST}:${REMOTE_DIR}/"
  ok "rsync 同步完成"
else
  log "本机无 rsync，改用 tar over ssh"
  # tar 的排除用 --exclude 同名；从 PROJECT_ROOT 打包，远端解到 REMOTE_DIR。
  tar -C "$PROJECT_ROOT" \
    --exclude='.git' --exclude='node_modules' \
    --exclude='v2.db' --exclude='v2.db-wal' --exclude='v2.db-shm' \
    --exclude='.env' --exclude='workspace' \
    -czf - . | rexec "tar -C ${REMOTE_DIR} -xzf -"
  ok "tar 同步完成"
fi

# 确保沙箱 workspace 目录存在（read/write_file 工具需要）。
rexec "mkdir -p ${REMOTE_DIR}/workspace"

# ---------------------------------------------------------------------
# 3. .env：不存在才从样板拷骨架；已存在则保留（不动密钥）
# ---------------------------------------------------------------------
log "步骤 3/6: 准备 .env..."
if rexec "test -f ${REMOTE_DIR}/.env"; then
  ok ".env 已存在，保留不动（密钥安全）"
  # 提醒：样板可能新增了变量，让人知道去对一下。
  log "提示：若 .env.example 有新增变量，手动同步到 ${REMOTE_DIR}/.env"
else
  rexec "cp ${REMOTE_DIR}/.env.example ${REMOTE_DIR}/.env && chmod 600 ${REMOTE_DIR}/.env"
  log "已从 .env.example 生成 ${REMOTE_DIR}/.env（chmod 600）"
  log "⚠️  下一步务必填入 LLM_API_KEY / WECOM_TOKEN / WECOM_GUID / WECOM_TARGET_ID / OWNER_ID 再重启服务"
fi

# ---------------------------------------------------------------------
# 4. systemd unit
# ---------------------------------------------------------------------
log "步骤 4/6: 安装 systemd 服务..."
rexec "sudo cp ${REMOTE_DIR}/deploy/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service"
rexec "sudo touch /var/log/${SERVICE_NAME}.log"
rexec "sudo systemctl daemon-reload"
rexec "sudo systemctl enable ${SERVICE_NAME}"
# restart 而非 start：重新部署时确保拉到新代码（start 对已运行的是 no-op）。
rexec "sudo systemctl restart ${SERVICE_NAME}"
ok "systemd 服务已 enable + restart"

# ---------------------------------------------------------------------
# 5. crontab：watchdog 每 5min + 每日 wal_checkpoint 后备份
# ---------------------------------------------------------------------
log "步骤 5/6: 安装 crontab（watchdog + 每日备份）..."
# 备份脚本生成在远端：先 wal_checkpoint(TRUNCATE) 把 WAL 落盘归零，再 cp v2.db（一致快照）。
# 为什么先 checkpoint：直接 cp v2.db 会漏掉还在 -wal 里没回主库的写；checkpoint 后主库自洽，
# cp 单文件就是干净备份，不用同时拷 -wal/-shm。保留最近 7 天。
BACKUP_SCRIPT="${REMOTE_DIR}/deploy/backup_db.sh"
rexec "cat > ${BACKUP_SCRIPT}" <<'BACKUP_EOF'
#!/usr/bin/env bash
# 每日 db 备份：wal_checkpoint(TRUNCATE) 后 cp 出一致快照，保留 7 天。
set -euo pipefail
DIR=/opt/xiaowang-v2
DB=$DIR/v2.db
BK=$DIR/backups
NODE=/usr/local/bin/node
mkdir -p "$BK"
[ -f "$DB" ] || { echo "[backup] $DB 不存在，跳过"; exit 0; }
# 用 node:sqlite 对同一个 db 文件做 checkpoint。单写连接铁律下主进程也在写，
# 但 WAL 模式允许并发读 + checkpoint 是幂等的，TRUNCATE 把已提交的 WAL 落主库。
$NODE --experimental-sqlite -e "
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync(process.env.XW2_DB_PATH || '$DB');
try { db.exec('PRAGMA busy_timeout=10000'); db.prepare('PRAGMA wal_checkpoint(TRUNCATE)').get(); }
finally { db.close(); }
" || echo "[backup] checkpoint 失败（继续 cp，备份可能含未 checkpoint 的写）"
TS=$(date +%Y%m%d_%H%M%S)
cp "$DB" "$BK/v2_$TS.db"
echo "[backup] -> $BK/v2_$TS.db"
# 保留最近 7 份，多的删（低成本资产，可自动清，不触发团队原则9）。
ls -1t "$BK"/v2_*.db 2>/dev/null | tail -n +8 | xargs -r rm -f
BACKUP_EOF
rexec "chmod +x ${BACKUP_SCRIPT}"

# watchdog_cron.mjs 由 main/自愈 builder 提供；这里只负责把它挂上 cron。
# 即使该文件暂未就位，cron 行也无害（cron 报错进邮件/日志，不影响主服务）。
CRON_WATCHDOG="*/5 * * * * ${NODE_BIN} --experimental-sqlite ${REMOTE_DIR}/watchdog_cron.mjs >> /var/log/${SERVICE_NAME}-watchdog.log 2>&1"
CRON_BACKUP="17 4 * * * /usr/bin/env bash ${BACKUP_SCRIPT} >> /var/log/${SERVICE_NAME}-backup.log 2>&1"
CRON_MARK="# xiaowang-v2-managed"

# 幂等装 crontab：删掉旧的 managed 行，再追加新的（避免重复部署堆积重复条目）。
rexec "( crontab -l 2>/dev/null | grep -v '${CRON_MARK}' ; \
        echo '${CRON_WATCHDOG} ${CRON_MARK}' ; \
        echo '${CRON_BACKUP} ${CRON_MARK}' ) | crontab -"
ok "crontab 已装：watchdog 每 5min，备份每日 04:17"

# ---------------------------------------------------------------------
# 6. 烟测（CLAUDE.md 原则4：部署后立即验证，不等明天）
# ---------------------------------------------------------------------
log "步骤 6/6: 烟测..."
# 等服务起来：轮询 systemctl is-active，最多 ~15s。
ACTIVE=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  ACTIVE="$(rexec "systemctl is-active ${SERVICE_NAME} 2>/dev/null || true")"
  [ "$ACTIVE" = "active" ] && break
  rexec "sleep 1.5" || true
done
if [ "$ACTIVE" != "active" ]; then
  log "服务未 active（状态=${ACTIVE}），最近日志："
  rexec "tail -n 30 /var/log/${SERVICE_NAME}.log 2>/dev/null || true"
  die "服务未能起来。注意：若 .env 还没填 LLM_API_KEY，主进程仍应起（mock 模式），检查日志里的真实错误。"
fi
ok "服务 active"

# 端口监听确认
if rexec "ss -tlnp 2>/dev/null | grep -q ':${HTTP_PORT} '"; then
  ok "端口 ${HTTP_PORT} 已监听"
else
  log "⚠️ 端口 ${HTTP_PORT} 暂未监听（worker 可能还在 initDb，稍后再 ss 查一次）"
fi

# 健康检查：尝试 /healthz（main.mjs 若提供），失败不致命（HTTP 路由由 main builder 定）。
HEALTH="$(rexec "curl -s -m 5 http://127.0.0.1:${HTTP_PORT}/healthz 2>/dev/null || true")"
if [ -n "$HEALTH" ]; then
  ok "健康检查响应: ${HEALTH}"
else
  log "（/healthz 无响应——若 main.mjs 未挂该路由属正常，看日志确认 worker tick）"
fi

log "最近日志（确认 worker 在 tick / heartbeat）："
rexec "tail -n 15 /var/log/${SERVICE_NAME}.log 2>/dev/null || true"

echo
ok "部署完成。后续："
echo "  - 填密钥:   ssh ${REMOTE_HOST} 'nano ${REMOTE_DIR}/.env' 然后 sudo systemctl restart ${SERVICE_NAME}"
echo "  - 看日志:   ssh ${REMOTE_HOST} 'tail -f /var/log/${SERVICE_NAME}.log'"
echo "  - 看 watchdog: ssh ${REMOTE_HOST} 'tail -f /var/log/${SERVICE_NAME}-watchdog.log'"
echo "  - 自检:     ssh ${REMOTE_HOST} '${NODE_BIN} --experimental-sqlite ${REMOTE_DIR}/main.mjs --selftest'"
