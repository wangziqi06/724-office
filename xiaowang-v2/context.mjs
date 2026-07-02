// =====================================================================
// context.mjs —— 不可感知连续性引擎·在线读路径（P0 核心）
//
// 职责（单一）：每条入站消息，纯同步 SQLite 组装"喂给 LLM 的上下文五层"，零额外 LLM 调用。
//   ① 逐字近窗 recentTurns —— 最近 N 轮 user/assistant 原文（防 tell #2 忘刚说的 / #4 重复问 / #6 当下指代）
//   ② 锚点台账 anchors      —— pinned 事实确定性全量（防 tell #3 自相矛盾）
//   ③ 运行摘要 summary      —— 早先对话脉络（防 tell #1 重置感；P0/P1 恒空，P3 接 compaction 才有）
//   ④ 语义召回 recalled     —— FTS/LIKE top-k 过相关性闸 + 媒体专项配额（防 tell #5 媒体 / #6 远程指代）
//   ⑤ recallWeak           —— 召回置信度低 → 让小王诚实 hedge，不编假连续
//
// 致命纪律：assembleContext 必须在 appendEpisode(user) 之【前】调用 —— 这样 recentTurns
//   物理上不含当前 userInput，userInput 只在 loop 的 buildMessages 末尾注入一次（杜绝双注入）。
//   单一排序轴 = episodes.id（致命纪律⑤）：近窗/边界全用 id 比较，ts 仅用于 recency 打分。
//
// 依赖方向（单向无环）：db/memory ← context。本模块只读，绝不写库、绝不调 LLM。
// 可整块删除（原则6 为删除而构建）：删掉它 + loop 退回纯召回即可。
// =====================================================================

import { getDb, nowMs, RECENT_TURNS_LIMIT, RELEVANCE_FLOOR, HEDGE_THRESHOLD } from './db.mjs';
import { retrieve, retrieveMedia, pinnedFacts, getSummary } from './memory.mjs';
import { getPending, isPendingExpired } from './esm.mjs';

/**
 * 组装一条入站消息的上下文五层。纯同步 SQLite 读，无副作用、无 LLM。
 * @param {string} sessionId
 * @param {string} userInput  当前这条用户输入（仅用于召回检索词；本函数不把它写库、不放进 recentTurns）
 * @returns {{recentTurns:Array<{role,content}>, summary:string, anchors:Array, recalled:Array, recallWeak:boolean}}
 */
export function assembleContext(sessionId, userInput) {
  const q = String(userInput ?? '');
  const sum = getSummary(sessionId); // 无摘要 → {summary:'', covers_until_id:0}

  // ① 逐字近窗（只取 user/assistant 文本；绝不取 role='tool'，否则裸 tool 消息进 messages 会 400）。
  const recentTurns = recentVerbatim(sessionId, sum.covers_until_id);

  // ② 锚点台账（pinned 事实全量，极少 token）。
  let anchors = [];
  try {
    anchors = pinnedFacts(12);
  } catch (e) {
    console.error('[context] pinnedFacts 失败，降级空锚点: %s', e.message);
  }

  // ④ 语义召回 + 相关性闸 + 媒体专项配额。失败要响但不致命（降级空召回，对话仍能跑）。
  let recalled = [];
  try {
    recalled = retrieve(q, 8, { sessionId }).filter((r) => relevanceGate(r));
  } catch (e) {
    console.error('[context] retrieve 失败，降级空召回: %s', e.message);
  }
  let mediaHits = [];
  try {
    mediaHits = retrieveMedia(q, 2, { sessionId });
  } catch (e) {
    console.error('[context] retrieveMedia 失败，降级空媒体召回: %s', e.message);
  }
  recalled = dedupMerge(recalled, mediaHits);

  // ⑤ 召回置信度：最高分低于阈值 → 标弱，让 prompt 切到"匹配较弱"标题、模型自然 hedge。
  const recallWeak =
    recalled.length > 0 && Math.max(...recalled.map((r) => r.score ?? 0)) < HEDGE_THRESHOLD;

  // ⑥ 待回打卡（原则11）：有未过期的晨/晚打卡 pending 时，确定性地把它注入上下文，
  //    让模型自己判断"子淇这条是不是在回打卡"→ 调 record_checkin（而非前置规则猜意图）。
  let pendingCheckin = null;
  try {
    const p = getPending(getDb());
    if (p && !isPendingExpired(p) && /_anchor$|_followup$/.test(p.type || '')) {
      pendingCheckin = { type: p.type };
    }
  } catch (e) {
    console.error('[context] pending 读取失败(忽略): %s', e.message);
  }

  // ⑦ 距上一轮的间隔（此刻当前消息尚未入库 → max(ts) 就是上一轮）：供 system prompt「现在」段注入。
  //    轮间时间盲是"隔几小时回来、被错误续接旧话题"的根因——人类靠微信 UI 的时间分割线免费获得这层感知。
  let sinceLastMs = null;
  try {
    const last = getDb()
      .prepare(`SELECT MAX(ts) t FROM episodes WHERE session_id = ? AND role IN ('user','assistant')`)
      .get(sessionId);
    if (last && last.t != null) sinceLastMs = Math.max(0, nowMs() - last.t);
  } catch (e) {
    console.error('[context] sinceLastMs 计算失败(忽略): %s', e.message);
  }

  return { recentTurns, summary: sum.summary, anchors, recalled, recallWeak, pendingCheckin, sinceLastMs };
}

// 轮间时间事实的标注阈值：相邻两轮隔 ≥ 30 分钟才标（更短是正常对话节奏，标了只是噪声）。
const GAP_MARK_MS = 30 * 60 * 1000;

// 间隔的人话表述（分钟/小时/天，粗粒度——模型要的是量级感，不是精度）。
function fmtGap(ms) {
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min}分钟`;
  const h = Math.round(ms / 3600000);
  if (h < 48) return `${h}小时`;
  return `${Math.round(ms / 86400000)}天`;
}

/**
 * 逐字近窗：会话内 id > coversUntilId 的最近 N 条 user/assistant 原文，按 id 升序（时间顺序）返回。
 * 单一排序轴 = id：> coversUntilId 保证不与运行摘要覆盖的段重叠（压缩段 ∩ 逐字窗 = ∅）。
 * @param {string} sessionId
 * @param {number} coversUntilId  运行摘要已覆盖到的 episode id 上界（无摘要时为 0）
 * @returns {Array<{role:string, content:string}>}
 */
export function recentVerbatim(sessionId, coversUntilId = 0) {
  const conn = getDb();
  // 排除 entity='media'：图片描述已随轮次装配进 user episode（逐字近窗有一份），
  // media episode 只作 retrieveMedia 的召回索引——不滤则同一描述在近窗出现两遍（占席位+模型误以为两张图）。
  const rows = conn
    .prepare(
      `SELECT id, role, content, ts
       FROM episodes
       WHERE session_id = ? AND id > ? AND role IN ('user','assistant')
         AND (entity IS NULL OR entity <> 'media')
       ORDER BY id DESC
       LIMIT ?`,
    )
    .all(sessionId, coversUntilId | 0, RECENT_TURNS_LIMIT);
  // DESC 取最近 N 条，再 reverse 成时间正序喂给 LLM（对话历史必须时间顺序）。
  // 轮间时间事实：隔 ≥ GAP_MARK_MS 的相邻两轮，在后一条前标〔隔了约X〕——人类在微信 UI 里
  // 免费看到的时间分割线，模型只能靠这里递进去；只标事实，"是不是新话题"归模型判断（原则11）。
  const chrono = rows.reverse();
  return chrono.map((r, i) => {
    const prev = chrono[i - 1];
    const gap = prev && r.ts - prev.ts >= GAP_MARK_MS ? `〔隔了约${fmtGap(r.ts - prev.ts)}〕\n` : '';
    return { role: r.role, content: gap + r.content };
  });
}

/**
 * 相关性闸：滤掉 score 过低的召回条目（关键词巧合命中的陈旧记忆）。
 * 起步只看 score 地板（RELEVANCE_FLOOR，标 emerging 待真实数据调）；
 * 实体重叠等更细的加权留给 backlog，首版宁简勿过早抽象（原则7）。
 * @param {{score?:number}} r
 * @returns {boolean} 保留?
 */
export function relevanceGate(r) {
  if (!r) return false;
  const s = typeof r.score === 'number' ? r.score : 0;
  return s >= RELEVANCE_FLOOR;
}

/**
 * 合并文本召回与媒体召回，按 episode id 去重（媒体专项条目若已在文本召回里则不重复）。
 * recalled 在前（语义相关优先），mediaHits 里未出现过的追加在后（保证媒体不被文本淹没）。
 */
export function dedupMerge(recalled, mediaHits) {
  const seen = new Set();
  const out = [];
  for (const r of [...(recalled || []), ...(mediaHits || [])]) {
    if (!r) continue;
    const key = r.id != null ? `id:${r.id}` : `c:${r.content}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(r);
  }
  return out;
}

// =====================================================================
// --selftest：离线、不联网，自建临时 db 验证五层组装的关键不变量。
// 必测：recentTurns 只取 user/assistant（不含 tool）、尊重 RECENT_TURNS_LIMIT、
//   尊重 covers_until_id 边界（压缩段∩逐字窗=∅）、sessionId 隔离、anchors 来自 pinned、
//   当前 userInput 不在 recentTurns（双注入防护的前提：assemble 不写库）。
// 运行：node context.mjs --selftest
// =====================================================================
import { pathToFileURL } from 'node:url';

async function runSelftest() {
  const { mkdtempSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const db = await import('./db.mjs');
  const mem = await import('./memory.mjs');

  let pass = 0;
  let fail = 0;
  const ok = (c, m) => {
    if (c) { pass++; console.log('  ✓ ' + m); }
    else { fail++; console.log('  ✗ ' + m); }
  };

  const dir = mkdtempSync(join(tmpdir(), 'xw2-ctx-'));
  const dbPath = join(dir, 'v2.db');
  process.env.XW2_DB_PATH = dbPath;

  try {
    const conn = db.initDb(dbPath);

    // 会话 A：穿插 user/assistant/tool；会话 B：另一条，验隔离。
    mem.appendEpisode({ sessionId: 'A', role: 'user', content: '提醒我下午三点开会' });
    mem.appendEpisode({ sessionId: 'A', role: 'assistant', content: '好，三点开会记下了' });
    mem.appendEpisode({ sessionId: 'A', role: 'tool', content: 'schedule_task → ok', entity: 'schedule_task' });
    mem.appendEpisode({ sessionId: 'A', role: 'user', content: '刚那个几点来着' });
    mem.appendEpisode({ sessionId: 'B', role: 'user', content: '另一个会话的话，不该串进来' });

    const ctx = assembleContext('A', '现在帮我看下日程');

    // recentTurns 只含 user/assistant，绝无 tool
    ok(ctx.recentTurns.every((t) => t.role === 'user' || t.role === 'assistant'), 'recentTurns 不含 role=tool（防 400）');
    ok(ctx.recentTurns.some((t) => t.content.includes('下午三点开会')), 'recentTurns 含会话内历史原文');
    // sessionId 隔离：B 的内容不串入
    ok(!ctx.recentTurns.some((t) => t.content.includes('另一个会话')), 'sessionId 隔离：别的会话不串进 recentTurns');
    // 当前 userInput 不在 recentTurns（assemble 不写库 → 双注入防护前提成立）
    ok(!ctx.recentTurns.some((t) => t.content.includes('现在帮我看下日程')), '当前 userInput 不在 recentTurns（防双注入前提）');
    // 时间正序：最后一条是最近的 user
    ok(ctx.recentTurns[ctx.recentTurns.length - 1].content.includes('刚那个几点'), 'recentTurns 时间正序（最近在末尾）');
    // media episode 不进逐字近窗（描述已随轮次装配进 user episode，这份只作 retrieveMedia 召回索引——防双写）
    mem.appendEpisode({ sessionId: 'A', role: 'user', content: '[media#99|图片] 一碗测试面', entity: 'media', taskId: 99 });
    const ctxM = assembleContext('A', 'x');
    ok(!ctxM.recentTurns.some((t) => t.content.includes('[media#99|图片]')), 'entity=media 不进 recentTurns（近窗防媒体描述双写）');
    ok((mem.retrieveMedia('测试面', 2, { sessionId: 'A' }) || []).some((h) => h.content.includes('media#99')), 'media episode 仍被 retrieveMedia 召回（索引职责不变）');

    // 轮间时间事实：≥30min 的相邻轮在后一条前标〔隔了约X〕；<30min 不标；sinceLastMs=距上一轮真实间隔
    db.__setClockForTest(() => Date.parse('2026-07-02T00:00:00Z'));
    mem.appendEpisode({ sessionId: 'T', role: 'user', content: '早上的话题' });
    mem.appendEpisode({ sessionId: 'T', role: 'assistant', content: '早上的回复' });
    db.__setClockForTest(() => Date.parse('2026-07-02T06:00:00Z')); // 6 小时后
    mem.appendEpisode({ sessionId: 'T', role: 'user', content: '下午的新话题' });
    db.__setClockForTest(() => Date.parse('2026-07-02T06:10:00Z')); // 再过 10 分钟（<30min）
    const ctxT = assembleContext('T', 'x');
    ok(ctxT.recentTurns.some((t) => t.content.startsWith('〔隔了约6小时〕\n下午的新话题')), '≥30min 间隔在后一条前标〔隔了约X〕（轮间时间盲根治）');
    ok(!ctxT.recentTurns[1].content.includes('〔隔了约'), '<30min 的相邻轮不标（不刷噪声）');
    ok(ctxT.sinceLastMs === 10 * 60 * 1000, 'sinceLastMs=距上一轮的真实间隔（供 system「现在」段）');
    ok(assembleContext('全新会话', 'x').sinceLastMs === null, '无历史的新会话 sinceLastMs=null');
    db.__setClockForTest(null);

    // covers_until_id 边界：插一条已覆盖到第2条的摘要，逐字窗应只剩 id>2 的
    db.tx((c) => {
      c.prepare(
        `INSERT INTO session_summaries (session_id, summary, covers_until_id, superseded, needs_review, updated_at)
         VALUES ('A', '子淇约了下午三点开会', 2, 0, 0, ?)`,
      ).run(db.nowMs());
    });
    const ctx2 = assembleContext('A', 'x');
    ok(ctx2.summary === '子淇约了下午三点开会', 'getSummary 取到最新有效摘要');
    ok(!ctx2.recentTurns.some((t) => t.content.includes('下午三点开会') && t.role === 'user'), 'covers_until_id 边界：被摘要覆盖的早段不再进逐字窗（压缩段∩逐字窗=∅）');
    ok(ctx2.recentTurns.some((t) => t.content.includes('刚那个几点')), '边界之后的逐字轮仍在窗内');

    // 锚点台账：pin 一条 fact，应进 anchors
    db.tx((c) => {
      c.prepare(
        `INSERT INTO facts (entity, fact, source, confidence, created_at, valid_from, importance, pinned)
         VALUES ('居住', '现居上海', 'user_said', 0.9, ?, ?, 0.9, 1)`,
      ).run(db.nowMs(), db.nowMs());
    });
    const ctx3 = assembleContext('A', 'x');
    ok(ctx3.anchors.some((a) => a.fact.includes('现居上海')), 'anchors 含 pinned 事实');

    // RECENT_TURNS_LIMIT 截断
    for (let i = 0; i < db.RECENT_TURNS_LIMIT + 10; i++) {
      mem.appendEpisode({ sessionId: 'C', role: i % 2 ? 'assistant' : 'user', content: `c轮${i}` });
    }
    const ctxC = assembleContext('C', 'x');
    ok(ctxC.recentTurns.length <= db.RECENT_TURNS_LIMIT, `recentTurns 截断到 RECENT_TURNS_LIMIT(${db.RECENT_TURNS_LIMIT})`);

    // relevanceGate / dedupMerge 纯函数
    ok(relevanceGate({ score: 1 }) === true && relevanceGate({ score: 0 }) === false, 'relevanceGate 按 score 地板过滤');
    const merged = dedupMerge([{ id: 1, content: 'a' }], [{ id: 1, content: 'a' }, { id: 2, content: 'b' }]);
    ok(merged.length === 2, 'dedupMerge 按 id 去重');
  } catch (e) {
    fail++;
    console.log('  ✗ selftest 异常: ' + e.stack);
  } finally {
    try { db.__closeForTest && (await import('./db.mjs')).__closeForTest(); } catch {}
    rmSync(dir, { recursive: true, force: true });
  }

  console.log(`\n[context.mjs selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

if (
  process.argv.includes('--selftest') &&
  import.meta.url === pathToFileURL(process.argv[1] || '').href
) {
  await runSelftest();
}
