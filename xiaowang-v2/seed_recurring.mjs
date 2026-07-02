// =====================================================================
// seed_recurring.mjs —— 幂等播种 canonical 周期任务（天气播报）。
//
// 为什么需要它：recurring_jobs 是运行时状态。之前天气播报是在服务器上手工
// addJob 播种的，repo 里没有记录 → 换机 / 重建库就丢、也没人知道"本该有哪些"。
// 本脚本是「小王该定时做哪些事」的单一可信源：新机 / 新库后跑一次即恢复。
//
// 幂等：按 name 去重，已存在的不重复建（可反复跑）。它【只增不删】——不碰
// 不在清单里的任务（如临时冒烟测试），也不复活被禁用的（enabled=0 的同名仍算存在 → 跳过）。
//
// ⚠️ 日常部署不要跑这个：若子淇用 cancel_schedule 删/禁了某条天气播报，
//    重跑 seed 不会复活它（按 name 已存在即跳过）——但也别依赖它来"重置"，它不是重置工具。
//    只在【全新库 / 换服务器】时跑一次。
//
// 跑法：node seed_recurring.mjs   （node24 自带 node:sqlite；老版本加 --experimental-sqlite）
// 依赖方向：db ← recurring ← seed（叶子脚本，可整块删除，原则6）。
// =====================================================================

import { pathToFileURL } from 'node:url';
import { initDb, getDb } from './db.mjs';
import { initRecurringSchema, listJobs, addJob } from './recurring.mjs';

// canonical 清单（与线上一致）。改这里 = 改"小王该定时做什么"的事实源。
// builtin 天气走确定性 weather.mjs（不经 LLM、无 AI 味）；preset 见 main.runRecurringBuiltin。
export const CANONICAL_JOBS = [
  { name: '天气-上海早', fireHm: '08:30', dow: null, kind: 'builtin', action: { handler: 'weather', preset: 'morning' } },
  { name: '天气-沪深晚', fireHm: '22:30', dow: null, kind: 'builtin', action: { handler: 'weather', preset: 'evening' } },
];

/**
 * 幂等播种：清单里缺的才建，已存在（同名）的跳过。返回 {added, skipped}。
 * @param {object} [db]
 */
export function seedRecurring(db = getDb()) {
  initRecurringSchema(db);
  const existing = new Set(listJobs(db).map((j) => j.name));
  const added = [];
  const skipped = [];
  for (const job of CANONICAL_JOBS) {
    if (existing.has(job.name)) { skipped.push(job.name); continue; }
    addJob(db, job);
    added.push(job.name);
  }
  return { added, skipped };
}

// CLI 入口。
if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  initDb();
  const r = seedRecurring(getDb());
  console.log('[seed_recurring] added:', r.added.length ? r.added.join('、') : '(无，全部已存在)');
  console.log('[seed_recurring] skipped(已存在):', r.skipped.length ? r.skipped.join('、') : '(无)');
  console.log('[seed_recurring] 当前所有周期任务:');
  console.log(JSON.stringify(listJobs(getDb()), null, 1));
}
