#!/usr/bin/env node
// =====================================================================
// watchdog_cron.mjs —— 进程外自愈看门狗（自愈原则，契约 §四样超能力 2）
//
// 独立可执行，crontab 每 5min 跑一次。职责单一：
//   1. 直接打开 v2.db（自己的连接，只读），读 heartbeat(id=1) 行。
//   2. ts 静默 > 10min → 判主进程卡死/死亡：
//        a. child_process systemctl restart <unit>
//        b. fetch 企微 API 直接报警（读 .env 自取凭证）。
//
// 为什么进程外 + 不依赖主进程模块状态（契约硬要求）：
//   - 主进程若卡死/OOM，主进程内的任何 setInterval/db.mjs 单例都不可信。
//   - watchdog 必须能在主进程完全失能时独立判断并拉起，故：
//       · 自己读 .env（不靠 process.env 被主进程注入）
//       · 自己开一个临时 DatabaseSync 只读连接（不复用 db.mjs 的单写单例）
//       · 报警 fetch 自带 timeout，绝不无限等待
//
// 退出码：0=健康或已成功处理；1=检测到异常并已尝试重启（便于 cron 日志/邮件区分）。
// systemd 侧仍配 Restart=always RestartSec=5（不配 WatchdogSec），watchdog 是第二道防线。
// =====================================================================

import { DatabaseSync } from 'node:sqlite';
import { execFile } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 本文件所在目录（契约：用 import.meta.dirname / fileURLToPath，不用 __dirname）。
const HERE = dirname(fileURLToPath(import.meta.url));

// ---- 阈值常量（契约 §常量集中）----
const SILENCE_MS = 10 * 60 * 1000; // heartbeat 静默 > 10min 判死
const ALERT_TIMEOUT_MS = 15000;    // 报警 fetch 超时（致命纪律②，绝不无限等待）

// ---------------------------------------------------------------------
// 自取 .env（不依赖主进程注入的 process.env）。
// 为什么自己解析：cron 环境的 env 通常很裸，主进程的 env 也可能已随其死亡丢失。
// 极简解析器：KEY=VALUE，忽略空行/#注释，去首尾引号。只取我们需要的几个键。
// process.env 已有的值优先（允许 cron 行内覆盖），其次 .env，最后内置默认。
// ---------------------------------------------------------------------
function loadEnv() {
  const env = {};
  const envPath = process.env.XW2_ENV_PATH || join(HERE, '.env');
  if (existsSync(envPath)) {
    try {
      const text = readFileSync(envPath, 'utf8');
      for (const rawLine of text.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line || line.startsWith('#')) continue;
        const eq = line.indexOf('=');
        if (eq < 0) continue;
        const key = line.slice(0, eq).trim();
        let val = line.slice(eq + 1).trim();
        // 去成对引号
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
          val = val.slice(1, -1);
        }
        env[key] = val;
      }
    } catch (e) {
      console.error('[watchdog] failed to read .env at %s: %s', envPath, e.message);
    }
  }
  // process.env 覆盖 .env（cron 行可显式传），返回合并视图的取值器。
  const get = (k, dflt = '') => (process.env[k] != null ? process.env[k] : env[k] != null ? env[k] : dflt);
  return { get, envPath };
}

// ---------------------------------------------------------------------
// 读 heartbeat 行。自己开只读连接（不复用 db.mjs 单例）。
// busy_timeout 设短一点（watchdog 不该被锁久等）；readOnly 防误写。
// 返回 { ts, progress } 或 null（无库/无行/读失败）。
// ---------------------------------------------------------------------
function readHeartbeat(dbPath) {
  if (!existsSync(dbPath)) {
    console.error('[watchdog] db not found at %s', dbPath);
    return null;
  }
  let db = null;
  try {
    // readonly 打开：主进程持有单写连接，watchdog 只读不抢写锁。
    db = new DatabaseSync(dbPath, { readOnly: true });
    // busy_timeout 提到 10s（与备份 checkpoint 的 10s 对齐）：每日 wal_checkpoint 期间读连接等得起，
    // 不因 3s 超时就误判 DOWN。watchdog 只读不持写锁，多等几秒无害。
    db.exec('PRAGMA busy_timeout=10000;');
    const row = db.prepare('SELECT ts, progress FROM heartbeat WHERE id = 1').get();
    return row ? { ts: Number(row.ts), progress: row.progress || '' } : null;
  } catch (e) {
    // 读失败（库锁死/损坏/WAL 异常）本身就是异常信号，响亮记录。
    console.error('[watchdog] heartbeat read failed: %s', e.message);
    return null;
  } finally {
    try { db?.close(); } catch { /* 忽略关闭异常 */ }
  }
}

// ---------------------------------------------------------------------
// systemctl restart（child_process，独立于主进程）。
// unit 名由 env 给（默认 xiaowang-v2）。带 timeout 防 systemctl 自身卡住。
// 非 Linux/无 systemctl（如本地 Windows 开发）时优雅降级：记录但不视为致命。
// ---------------------------------------------------------------------
async function restartService(unit) {
  try {
    const { stdout, stderr } = await execFileAsync('systemctl', ['restart', unit], { timeout: 20000 });
    console.error('[watchdog] systemctl restart %s ok. %s%s', unit, stdout || '', stderr || '');
    return true;
  } catch (e) {
    // 可能：无 systemctl（Windows）/ 无权限 / unit 不存在。报警里也会带上这个失败。
    console.error('[watchdog] systemctl restart %s failed: %s', unit, e.message);
    return false;
  }
}

// ---------------------------------------------------------------------
// 带 timeout 的 fetch（致命纪律②）。watchdog 报警绝不能无限等待，否则 cron 任务堆积。
// ---------------------------------------------------------------------
async function fetchWithTimeout(url, opts, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new Error('timeout')), timeoutMs);
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------
// 直接 fetch 企微 API 报警（不走 outbox——主进程可能死了，outbox relay 不会跑）。
// 沿用 e云企微(QIWE) doApi /msg/sendText 格式（与 adapter.sendWecom 同一体系）。
// 凭证全部从 loadEnv 取（不依赖主进程模块状态）。失败响亮记录，不静默吞。
// ---------------------------------------------------------------------
async function alertWecom(get, message) {
  const apiUrl = get('WECOM_API_URL', 'http://manager.qiweapi.com/qiwe/api/qw/doApi');
  const token = get('WECOM_TOKEN');
  const guid = get('WECOM_GUID');
  const target = get('WECOM_TARGET_ID');

  if (!token || !guid || !target) {
    console.error('[watchdog] alert skipped: missing WECOM_TOKEN/WECOM_GUID/WECOM_TARGET_ID');
    return false;
  }
  try {
    const r = await fetchWithTimeout(
      apiUrl,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8', 'X-QIWEI-TOKEN': token },
        body: JSON.stringify({ method: '/msg/sendText', params: { guid, toId: String(target), content: message } }),
      },
      ALERT_TIMEOUT_MS
    );
    const j = await r.json().catch(() => ({}));
    if (j.code !== undefined && j.code !== 0) {
      console.error('[watchdog] alert api code=%s msg=%s', j.code, j.msg || '');
      return false;
    }
    console.error('[watchdog] alert sent to owner.');
    return true;
  } catch (e) {
    console.error('[watchdog] alert fetch failed: %s', e.message);
    return false;
  }
}

// ---------------------------------------------------------------------
// 主流程：读 heartbeat → 判静默 → 若死则 restart + alert。
// ---------------------------------------------------------------------
async function run() {
  const { get } = loadEnv();
  const dbPath = get('XW2_DB_PATH', join(HERE, 'v2.db'));
  const unit = get('XW2_SYSTEMD_UNIT', 'xiaowang-v2');
  const now = Date.now(); // watchdog 独立进程，直接用系统时钟（不依赖 db.nowMs 模块）

  // 去抖：单次读失败可能只是每日备份的 wal_checkpoint 等瞬时争用，不立刻判死——
  // 同一次 cron 运行内快速重试几次（间隔 2s），都读不到才升级为 DOWN，避免误杀健康进程 + 假告警轰炸。
  let hb = readHeartbeat(dbPath);
  for (let i = 0; hb == null && i < 3; i++) {
    await sleep(2000);
    hb = readHeartbeat(dbPath);
  }

  // 情况 A：重试后仍读不到 heartbeat（无行/库损坏/持续锁死）= 强异常信号 → restart + alert。
  if (!hb) {
    console.error('[watchdog] no readable heartbeat after retries → treating as DOWN.');
    const restarted = await restartService(unit);
    await alertWecom(
      get,
      `🃏 小王 v2 看门狗告警：读不到 heartbeat（库缺失/锁死/无行）。已尝试 systemctl restart ${unit}（${restarted ? '成功' : '失败'}）。时间 ${new Date(now).toISOString()}`
    );
    process.exit(1);
  }

  const silentMs = now - hb.ts;

  // 情况 B：静默超阈值 → 判主进程卡死 → restart + alert。
  if (silentMs > SILENCE_MS) {
    const silentMin = Math.round(silentMs / 60000);
    console.error('[watchdog] heartbeat silent %dmin (>%dmin) → DOWN. last progress: %s', silentMin, SILENCE_MS / 60000, hb.progress);
    const restarted = await restartService(unit);
    await alertWecom(
      get,
      `🃏 小王 v2 看门狗告警：心跳静默 ${silentMin} 分钟（阈值 ${SILENCE_MS / 60000} 分钟）。最后进度「${hb.progress}」。已尝试 systemctl restart ${unit}（${restarted ? '成功' : '失败'}）。`
    );
    process.exit(1);
  }

  // 情况 C：健康。安静退出（cron 不刷屏，仅 stderr 一行便于排障）。
  console.error('[watchdog] healthy. heartbeat %ds ago. progress: %s', Math.round(silentMs / 1000), hb.progress);
  process.exit(0);
}

// =====================================================================
// --selftest：离线验证判活逻辑（不真 restart、不真发）。
// 用临时 db（XW2_DB_PATH 指 scratchpad 临时文件），自己建 heartbeat 行，
// 断言：新鲜心跳=healthy(exit0语义)、陈旧心跳=DOWN(触发 restart+alert 语义)。
// 为避免真调 systemctl/fetch，selftest 直接测纯函数 readHeartbeat + 阈值判断，不进 run() 的 process.exit。
// =====================================================================
async function selftest() {
  let pass = 0;
  let fail = 0;
  const ok = (cond, msg) => {
    if (cond) { pass++; console.log('  ✓ ' + msg); }
    else { fail++; console.log('  ✗ ' + msg); }
  };

  console.log('watchdog selftest (offline, no restart/no send)\n');

  const dbPath = process.env.XW2_DB_PATH || join(HERE, 'watchdog_selftest.db');

  // 自己建一个最小 heartbeat 表 + 行（不依赖 db.mjs，验证 watchdog 自给自足）。
  const wdb = new DatabaseSync(dbPath);
  wdb.exec('PRAGMA journal_mode=WAL;');
  wdb.exec(`CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY CHECK (id=1), ts INTEGER NOT NULL, progress TEXT);`);

  const now = Date.now();

  // 1) 新鲜心跳 → healthy（silentMs < 阈值）。
  wdb.prepare(`INSERT INTO heartbeat (id, ts, progress) VALUES (1, ?, 'fresh')
               ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, progress=excluded.progress`).run(now);
  wdb.close();
  let hb = readHeartbeat(dbPath);
  ok(hb != null, 'readHeartbeat reads the row back');
  ok(hb && now - hb.ts < SILENCE_MS, 'fresh heartbeat → silentMs < SILENCE_MS (healthy)');

  // 2) 陈旧心跳 → DOWN（silentMs > 阈值）。
  const wdb2 = new DatabaseSync(dbPath);
  wdb2.prepare(`UPDATE heartbeat SET ts = ? WHERE id = 1`).run(now - (SILENCE_MS + 60000));
  wdb2.close();
  hb = readHeartbeat(dbPath);
  ok(hb && now - hb.ts > SILENCE_MS, 'stale heartbeat (>10min) → silentMs > SILENCE_MS (DOWN, would restart+alert)');

  // 3) loadEnv：能从 process.env 取到值（覆盖 .env）。
  process.env.WD_SELFTEST_KEY = 'xyz';
  const { get } = loadEnv();
  ok(get('WD_SELFTEST_KEY') === 'xyz', 'loadEnv get() reads process.env override');
  ok(get('NONEXISTENT_KEY', 'fallback') === 'fallback', 'loadEnv get() returns default for missing key');

  // 4) 缺凭证时 alertWecom 安全跳过（返回 false，不抛、不真发）。
  const alerted = await alertWecom((k, d = '') => (k === 'WECOM_TOKEN' ? '' : d), 'test');
  ok(alerted === false, 'alertWecom skips safely when credentials missing');

  console.log(`\nPASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

// CLI 入口。watchdog 是独立可执行，总是作为主模块运行（cron 直接 `node watchdog_cron.mjs`），
// 无需 import.meta.url 比对守卫。
if (process.argv.includes('--selftest')) {
  selftest().catch((e) => {
    console.error('[watchdog] selftest crashed: %s\n%s', e.message, e.stack);
    process.exit(1);
  });
} else {
  run().catch((e) => {
    // run 内部已处理各分支；这里兜未预期异常（视为异常，退出码 1）。
    console.error('[watchdog] crashed: %s\n%s', e.message, e.stack);
    process.exit(1);
  });
}
