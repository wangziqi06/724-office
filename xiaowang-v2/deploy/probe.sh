#!/usr/bin/env bash
# =====================================================================
# 小王 v2 — 部署前能力探针 (动手前的第一件事)
#
# 为什么先跑这个：整个 v2 押注 node:sqlite 内置 + FTS5 + WAL+synchronous=FULL。
# 这三样里任何一样在目标机不可用，db.mjs / memory.mjs 的设计就要降级或推翻。
# 与其部署后在生产里炸，不如部署前 30 秒在服务器上跑一遍真实环境，拿 PASS/FAIL。
#
# 跑什么：
#   1. node --version （契约要 Node24，本地 Node22 也认）
#   2. node:sqlite 能 import（DatabaseSync 存在）
#   3. WAL 真能开（PRAGMA journal_mode=WAL 返回 'wal'，不是被静默拒成 'delete'）
#   4. synchronous=FULL + busy_timeout=5000 不报错
#   5. FTS5 虚表能建 + MATCH 能查（决定 MEMORY_MODE='fts5' 还是降级 'like'）
#
# 默认在 ecs-wecom 上跑（生产目标机）。传 --local 在本机跑。
# 退出码：全 PASS=0，任一 FAIL=1（CI / install.sh 可据此 gate）。
# =====================================================================
set -uo pipefail

REMOTE_HOST="${XW2_PROBE_HOST:-ecs-wecom}"
RUN_LOCAL=0
for arg in "$@"; do
  case "$arg" in
    --local) RUN_LOCAL=1 ;;
    --host=*) REMOTE_HOST="${arg#--host=}" ;;
    -h|--help)
      echo "用法: probe.sh [--local] [--host=ecs-wecom]"
      echo "  默认 ssh 到 \$REMOTE_HOST 跑能力探针；--local 在本机跑。"
      exit 0 ;;
  esac
done

# ---- 探针正文：一段自包含的 node 脚本，建临时 db 真测，跑完自清理 ----
# 用 require('node:sqlite') 而非 import，因为 -e 单行更稳，且老 Node 需要 --experimental-sqlite。
# 每项打 [PASS]/[FAIL]，脚本末尾按 FAIL 计数决定 process.exit 码。
read -r -d '' PROBE_JS <<'NODEJS'
const fs = require('node:fs');

let failures = 0;
function check(name, ok, detail) {
  const tag = ok ? '[PASS]' : '[FAIL]';
  if (!ok) failures++;
  console.log(`${tag} ${name}${detail ? ' — ' + detail : ''}`);
}

// node:sqlite 可加载？
let DatabaseSync;
try {
  ({ DatabaseSync } = require('node:sqlite'));
  check('node:sqlite import', typeof DatabaseSync === 'function');
} catch (e) {
  check('node:sqlite import', false, e.message);
  // 没有 sqlite 后面全无意义，直接退出
  console.log(`\nPROBE RESULT: FAIL (${failures} failed) — node:sqlite 不可用，无法继续`);
  process.exit(1);
}

const dbPath = `/tmp/xw2_probe_${process.pid}.db`;
function rmAll() {
  for (const suf of ['', '-wal', '-shm', '-journal']) {
    try { fs.unlinkSync(dbPath + suf); } catch { /* 不存在就算了 */ }
  }
}
rmAll();

let db;
try {
  db = new DatabaseSync(dbPath);
} catch (e) {
  check('open DatabaseSync', false, e.message);
  console.log(`\nPROBE RESULT: FAIL (${failures} failed)`);
  process.exit(1);
}
check('open DatabaseSync', true, dbPath);

// WAL：必须真返回 'wal'。某些只读挂载/网络盘会静默降级，那对单写持久化是隐患。
try {
  const row = db.prepare('PRAGMA journal_mode=WAL').get();
  const mode = row && row.journal_mode;
  check('PRAGMA journal_mode=WAL', mode === 'wal', `got '${mode}'`);
} catch (e) {
  check('PRAGMA journal_mode=WAL', false, e.message);
}

// synchronous=FULL + busy_timeout=5000：契约要求，崩溃一致性靠这个。
try {
  db.exec('PRAGMA synchronous=FULL');
  db.exec('PRAGMA busy_timeout=5000');
  const sync = db.prepare('PRAGMA synchronous').get();
  // synchronous=FULL 对应数值 2
  check('PRAGMA synchronous=FULL', sync && Number(sync.synchronous) === 2, `got ${sync && sync.synchronous}`);
} catch (e) {
  check('PRAGMA synchronous=FULL / busy_timeout', false, e.message);
}

// FTS5：建虚表 + 写 + MATCH 查回。这一项决定 memory.mjs 用 fts5 还是降级 LIKE。
try {
  db.exec(`CREATE VIRTUAL TABLE probe_fts USING fts5(content, tokenize='unicode61')`);
  db.exec(`INSERT INTO probe_fts(content) VALUES ('hello durable world')`);
  const hit = db.prepare('SELECT content FROM probe_fts WHERE probe_fts MATCH ?').get('durable');
  check('FTS5 create + MATCH', !!(hit && /durable/.test(hit.content)), hit ? hit.content : 'no match');
} catch (e) {
  // FAIL 在这里不是致命：v2 设计本就支持降级 LIKE，但要让人知道会降级。
  check('FTS5 create + MATCH', false, e.message + ' (memory 将降级 LIKE)');
}

try { db.close(); } catch { /* 关不上也无所谓，进程要退了 */ }
rmAll();

console.log(`\nPROBE RESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} failed)`);
process.exit(failures === 0 ? 0 : 1);
NODEJS

# Node24 内置 node:sqlite 已稳定，老版本需要 --experimental-sqlite。
# 两个 flag 都带上：新版忽略未知的 experimental flag（其实仍接受），老版需要它。
# 用 --experimental-sqlite 在 Node24 上是 no-op，安全。
NODE_FLAGS="--experimental-sqlite"

run_probe() {
  local where="$1"
  echo "================================================================"
  echo " 小王 v2 能力探针 @ ${where}"
  echo "================================================================"
}

if [ "$RUN_LOCAL" -eq 1 ]; then
  run_probe "本机 (local)"
  if ! command -v node >/dev/null 2>&1; then
    echo "[FAIL] 本机找不到 node"
    exit 1
  fi
  echo "[INFO] node $(node --version)"
  echo "----------------------------------------------------------------"
  node $NODE_FLAGS -e "$PROBE_JS"
  exit $?
else
  run_probe "${REMOTE_HOST} (remote via ssh)"
  # 先确认 ssh 通 + node 在
  NODE_VER="$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$REMOTE_HOST" 'command -v node >/dev/null 2>&1 && node --version || echo NO_NODE' 2>&1)"
  if [ "$NODE_VER" = "NO_NODE" ]; then
    echo "[FAIL] ${REMOTE_HOST} 上找不到 node"
    exit 1
  fi
  if echo "$NODE_VER" | grep -qiE 'permission denied|connection|timed out|could not resolve|no route'; then
    echo "[FAIL] 无法 ssh 到 ${REMOTE_HOST}: ${NODE_VER}"
    exit 1
  fi
  echo "[INFO] ${REMOTE_HOST} node ${NODE_VER}"
  echo "----------------------------------------------------------------"
  # 把探针脚本通过 ssh stdin 传过去跑，避免引号地狱（CLAUDE.md SSH 命令注意）。
  printf '%s\n' "$PROBE_JS" | ssh -o ConnectTimeout=10 "$REMOTE_HOST" "node $NODE_FLAGS -e \"\$(cat)\""
  exit $?
fi
