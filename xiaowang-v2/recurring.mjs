// =====================================================================
// recurring.mjs —— 周期性任务调度（恢复自旧小王 scheduler.py 的"触发后重置 last_run"思路）。
//
// v2 原本只有一次性 schedule_task；这里补上"每天/每周固定时刻做 X"的共同底座——
// 天气播报、健康日报、新闻、日记提醒…一切 recurring 都长在它上面。任务是数据(表里的行)，机制是一次性投资。
//
// 设计取舍（少即是多 + 零依赖）：不引 cron-parser。日/周固定时刻用 'HH:MM'(CST) + 可选 dow，
// 复用 ESM 排程同款"纯读判定 → 到点/补发窗内未触发 → 触发"纪律。需要更复杂 cron 时再升级。
//
// 依赖方向（单向无环）：db ← recurring。本模块只读写自己的表 + 判定到期，【不执行动作】——
// 动作执行(发文案 / 派 agentic task)由 main 在 tick 里做（与 ESM esmDuePrompt 同构，保持 db←recurring 干净）。
// 可整块删除（原则6）：删本文件 + main 的 step②.6 即退回无周期任务。
// =====================================================================

import { getDb, nowMs, tx } from './db.mjs';

// 补发上限：到点后最多迟这么多分钟内仍补发（与 ESM 一致）；超了视为当天错过，不在离谱钟点补发。
const MAX_CATCHUP_LATE_MIN = 180;

// CST 墙钟（与 esm.mjs 同口径：epoch ms +8h 再取 getUTC*，不依赖 OS 时区）。
function cstParts() {
  const d = new Date(nowMs() + 8 * 3600 * 1000);
  const hm = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
  return { hm, date: d.toISOString().slice(0, 10), dow: d.getUTCDay() }; // dow: 0=周日
}
const hmToMin = (hm) => { const [h, m] = String(hm).split(':').map(Number); return h * 60 + m; };

// ---- schema：recurring_jobs（幂等建表） ----
export function initRecurringSchema(db = getDb()) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS recurring_jobs (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      name            TEXT    NOT NULL,
      fire_hm         TEXT    NOT NULL,                 -- 'HH:MM' CST
      dow             INTEGER,                          -- 0-6(0=周日) 周几触发；NULL=每天
      kind            TEXT    NOT NULL DEFAULT 'agentic' CHECK (kind IN ('agentic','builtin')),
      action          TEXT    NOT NULL DEFAULT '{}',    -- builtin:{handler,params} / agentic:{message}
      enabled         INTEGER NOT NULL DEFAULT 1,
      last_fired_date TEXT,                             -- 'YYYY-MM-DD' CST，当天只触发一次
      created_at      INTEGER NOT NULL
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_recurring_enabled ON recurring_jobs (enabled);`);
  return db;
}

function parseAction(s) {
  try { return JSON.parse(s) || {}; } catch { return {}; }
}

// ---- 到期判定（纯读，不写）：返回本拍应触发的 job（action 已 parse）。 ----
// 触发条件：enabled + (dow 为空=每天 或 dow 命中今天) + 已到点(hm>=fire) + 在补发窗内 + 当天未触发过。
export function dueRecurringJobs(db = getDb()) {
  const { hm, date, dow } = cstParts();
  const nowMin = hmToMin(hm);
  const rows = db.prepare(`SELECT * FROM recurring_jobs WHERE enabled = 1`).all();
  const due = [];
  for (const r of rows) {
    if (r.dow != null && r.dow !== dow) continue;
    const late = nowMin - hmToMin(r.fire_hm);
    if (late < 0 || late > MAX_CATCHUP_LATE_MIN) continue;
    if (r.last_fired_date === date) continue; // 当天已触发
    due.push({ ...r, action: parseAction(r.action), _date: date });
  }
  return due;
}

// 触发后落标（当天只触发一次）。与 enqueue/派 task 解耦：main 先成功执行动作再调它（失败则下拍重试）。
export function markJobFired(db, id, date) {
  tx((d) => {
    d.prepare(`UPDATE recurring_jobs SET last_fired_date = ? WHERE id = ?`).run(date, id);
  });
}

// ---- 管理接口（供 seed 脚本 / 未来的 schedule_recurring 工具用） ----
export function addJob(db, { name, fireHm, dow = null, kind = 'agentic', action = {} }) {
  if (!name || !fireHm) throw new Error('addJob: name 与 fireHm 必填');
  if (!/^\d{1,2}:\d{2}$/.test(fireHm)) throw new Error(`addJob: fireHm 需 'HH:MM'，得到 ${fireHm}`);
  if (kind !== 'agentic' && kind !== 'builtin') throw new Error(`addJob: kind 须 agentic|builtin`);
  return tx((d) => {
    const info = d.prepare(
      `INSERT INTO recurring_jobs (name, fire_hm, dow, kind, action, enabled, created_at)
       VALUES (?, ?, ?, ?, ?, 1, ?)`
    ).run(name, fireHm, dow, kind, JSON.stringify(action ?? {}), nowMs());
    return Number(info.lastInsertRowid);
  });
}

export function listJobs(db = getDb()) {
  return db.prepare(`SELECT id,name,fire_hm,dow,kind,enabled,last_fired_date FROM recurring_jobs ORDER BY fire_hm`).all();
}
export function removeJob(db, id) {
  return tx((d) => d.prepare(`DELETE FROM recurring_jobs WHERE id = ?`).run(id).changes);
}
export function setEnabled(db, id, enabled) {
  return tx((d) => d.prepare(`UPDATE recurring_jobs SET enabled = ? WHERE id = ?`).run(enabled ? 1 : 0, id).changes);
}

// =====================================================================
// --selftest：离线、临时 db，验建表/加任务/到期判定/落标幂等/补发窗。用可注入时钟钉死"现在"。
// =====================================================================
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { rmSync } from 'node:fs';

if (process.argv.includes('--selftest') && import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  const db = await import('./db.mjs');
  let pass = 0, fail = 0;
  const ok = (c, m) => { console.log(`  ${c ? '✓' : '✗'} ${m}`); c ? pass++ : fail++; };
  console.log('recurring.mjs selftest (临时 db, 注入时钟)\n');

  const tmp = join(tmpdir(), `xw2-rec-${process.pid}-${Date.now()}.db`);
  process.env.XW2_DB_PATH = tmp;
  try {
    const conn = db.initDb(tmp);
    initRecurringSchema(conn);

    const wid = addJob(conn, { name: '天气-上海早', fireHm: '08:30', dow: null, kind: 'builtin', action: { handler: 'weather', preset: 'morning' } });
    const sun = addJob(conn, { name: '周日回顾', fireHm: '21:00', dow: 0, kind: 'agentic', action: { message: '回顾这周' } });
    ok(wid > 0 && sun > 0, 'addJob 返回 id');
    ok(listJobs(conn).length === 2, 'listJobs 列出 2 条');

    // 钉死到 2026-06-26(周五) 08:45 CST（08:30 已过、在 180min 补发窗内、当天未触发）
    db.__setClockForTest(() => Date.parse('2026-06-26T00:45:00Z'));
    let due = dueRecurringJobs(conn);
    ok(due.length === 1 && due[0].id === wid, '08:45 周五：每日天气 job 到期（周日 job 因 dow 不命中不触发）');
    ok(due[0].action.handler === 'weather' && due[0].action.preset === 'morning', 'action 已 parse');

    markJobFired(conn, wid, due[0]._date);
    ok(dueRecurringJobs(conn).length === 0, '落标后当天同 job 不再触发（幂等）');

    // 超补发窗：12:00 CST 已超 08:30+180min → 不触发
    db.__setClockForTest(() => Date.parse('2026-06-26T04:00:00Z')); // 12:00 CST
    ok(dueRecurringJobs(conn).length === 0, '超 180min 补发窗不触发（不在离谱钟点补发）');

    // 周日 dow 命中：钉到 2026-06-28(周日) 21:05 CST
    db.__setClockForTest(() => Date.parse('2026-06-28T13:05:00Z'));
    due = dueRecurringJobs(conn);
    ok(due.some((j) => j.id === sun), '周日 21:05：dow=0 的周日 job 触发');

    // disable 后不触发
    setEnabled(conn, sun, false);
    ok(!dueRecurringJobs(conn).some((j) => j.id === sun), 'disable 后不触发');

    // removeJob
    ok(removeJob(conn, wid) === 1 && listJobs(conn).length === 1, 'removeJob 删除生效');

    db.__setClockForTest(null);
  } catch (e) {
    fail++; console.log('  ✗ selftest 异常: ' + e.stack);
  } finally {
    try { (await import('./db.mjs')).__closeForTest(); } catch {}
    for (const ext of ['', '-wal', '-shm']) { try { rmSync(tmp + ext, { force: true }); } catch {} }
  }
  console.log(`\n[recurring.mjs selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}
