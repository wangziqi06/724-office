// =====================================================================
// esm.mjs —— ESM 主观采集（从 digital-twin/esm.mjs 移植 + v2 胶水）
//
// ESM = Experience Sampling：晨/晚定时打卡 + # 快记 + 周回顾。它是 agent 【前面】的一层
// 确定性拦截——只在"你在回早晚问候"或"# 开头"这两个时刻接管走结构化采集；其它自由对话照常进 agent。
// 这么设计是为了【靠结构守红线】（团队原则2）：ESM 铁律「只问不评」，而 agent 天性想帮你分析——
// 用确定性拦截把采集和聊天隔开，agent 的热心永远碰不到 ESM 数据，红线由构造保证。
//
// 两层（与 twin 同源的诚实数据基因）：
//  1) 不可逆层 esm_raw：原话 + 精确时间戳，落地即存，永不依赖 LLM——解析失败也丢不了。
//  2) 可再生层 esm_coded：LLM 演绎编码，可重跑覆盖，绝不反向写 raw；只抽明确出现的值，没提到=null。
//  3) 守查看端：不产出 streak/连续天数/即时解读（assertNoGamification 结构级封死）。
//
// 依赖方向（单向无环）：db/memory ← esm。绝不 import adapter/main（回执由 adapter 发，本模块只返数据）。
// 可整块删除（原则6）：删本文件 + adapter ESM 拦截 + main ESM 排程即退回纯 agent。
// =====================================================================

import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { getDb, nowMs, tx } from './db.mjs';
import { appendEpisode, appendNote } from './memory.mjs';
import { IDENTITY } from './identity.mjs';

const DIR = import.meta.dirname;

// ---- env（读 v2 的 .env：LLM key + 晨晚/回顾时间 + owner） ----
function loadEnv() {
  const p = join(DIR, '.env');
  const env = { ...process.env };
  if (existsSync(p)) {
    for (const line of readFileSync(p, 'utf8').split('\n')) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m && !line.trim().startsWith('#')) env[m[1]] = m[2].trim();
    }
  }
  return env;
}
const ENV = loadEnv();
const USE_MOCK = ENV.LLM_MOCK === '1' || !ENV.LLM_API_KEY;
const MORNING = ENV.MORNING_TIME || '08:30';
const EVENING = ENV.EVENING_TIME || '22:30';
const REVIEW = ENV.REVIEW_TIME || '21:00';

// ---- 时间（CST）。runtime 模块，可用 Date（v2 时钟口径=epoch ms，ESM ts_local 用 CST ISO 便于按日切片） ----
const cstShift = () => new Date(nowMs() + 8 * 3600 * 1000);
const cstIso = () => cstShift().toISOString().replace('Z', '+08:00');
function cstParts() {
  const d = cstShift();
  const hm = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
  return { hm, date: d.toISOString().slice(0, 10), dow: d.getUTCDay() };
}

// ============ 提示词模板（发给用户；含"可无视权"，答完不给解读）============
export const PROMPTS = {
  morning_anchor:
`早～醒了的话：
昨晚睡得怎么样？(1=很差 5=很好)
夜里被弄醒过吗？此刻有劲还是困？(1=很困 5=很精神)
（想说啥补一句；忙就不用回，留着就行）`,
  evening_anchor:
`睡前打个卡～
1) 今天整体压力 1-5？情绪一个词？
2) 喝酒(无/1-2/3+)？咖啡因末次(无/上午/下午/晚)？运动(无/上午/下午/睡前3h)？
3) 今天有没有异常：生病 / 熬夜补觉倒时差 / 出差 / 发生大事？
   —— 没有就回"都没有"，有就一句话。
（不想回今天就算了）`,
};

// ============ 编码 codebook ============
const ENUM = {
  alcohol: ['none', '1-2', '3+'],
  caffeine: ['none', 'morning', 'afternoon', 'evening'],
  exercise: ['none', 'morning', 'afternoon', 'within_3h_sleep'],
};
const clampScore = (v) => (Number.isInteger(v) && v >= 1 && v <= 5 ? v : null);
const asBool01 = (v) => (v === true ? 1 : v === false ? 0 : null);
const inEnum = (v, k) => (ENUM[k].includes(v) ? v : null);

// ============ schema（esm 三表 + bot_state；db.mjs 已设 PRAGMA，这里不重复） ============
export function initEsmSchema(db = getDb()) {
  db.exec(`CREATE TABLE IF NOT EXISTS esm_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_local TEXT NOT NULL,
    prompt_id TEXT NOT NULL CHECK (prompt_id IN
      ('morning_anchor','evening_anchor','morning_followup','evening_followup')),
    parent_id INTEGER REFERENCES esm_raw(id),
    raw_text TEXT,
    skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0,1))
  );`);
  db.exec('CREATE INDEX IF NOT EXISTS idx_esm_raw_ts ON esm_raw(ts_local);');
  db.exec(`CREATE TABLE IF NOT EXISTS esm_coded (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id INTEGER NOT NULL REFERENCES esm_raw(id) ON DELETE CASCADE,
    construct TEXT NOT NULL, value_num INTEGER, value_text TEXT, model TEXT, coded_at TEXT
  );`);
  db.exec('CREATE INDEX IF NOT EXISTS idx_esm_coded_raw ON esm_coded(raw_id);');
  db.exec(`CREATE TABLE IF NOT EXISTS daily_events (
    date TEXT PRIMARY KEY,
    alcohol TEXT, caffeine TEXT, exercise TEXT, anomaly_note TEXT,
    raw_id INTEGER REFERENCES esm_raw(id), coded_at TEXT
  );`);
  db.exec(`CREATE TABLE IF NOT EXISTS bot_state (k TEXT PRIMARY KEY, v TEXT);`);
  assertNoGamification(db);
  return db;
}

// 红线结构断言：查看端游戏化列（streak/连续天数/完成率…）结构级封死。
function assertNoGamification(db) {
  const FORBIDDEN = /streak|completion|progress|consecutive|days_in_a_row|nag/i;
  for (const t of ['esm_raw', 'esm_coded', 'daily_events']) {
    const cols = db.prepare(`PRAGMA table_info(${t})`).all().map((r) => r.name);
    const bad = cols.filter((c) => FORBIDDEN.test(c));
    if (bad.length) throw new Error(`红线: 表 ${t} 出现游戏化列 ${bad.join(',')}（查看端纪律）`);
  }
}

// ============ 采集：不可逆的一步，永不调 LLM ============
export function capture(db, { promptId, rawText, ts, skipped = false, parentId = null }) {
  const info = db.prepare(
    'INSERT INTO esm_raw (ts_local, prompt_id, parent_id, raw_text, skipped) VALUES (?,?,?,?,?)'
  ).run(ts, promptId, parentId, skipped ? null : (rawText ?? '').trim(), skipped ? 1 : 0);
  return Number(info.lastInsertRowid);
}

// ============ 编码：可再生，从 raw 演绎结构化值，幂等重跑 ============
function coderMessages(promptId, rawText) {
  const head = '你是一个"演绎编码器"。把用户的一句话回复，按固定 codebook 抽成结构化值。' +
    '铁律：只抽**明确出现**的值；没提到的构念一律 null，**绝不编造、不脑补、不推断**。严格只输出 JSON，不要解释。';
  const book = promptId === 'morning_anchor'
    ? ['这是【晨起自检】回复。codebook：',
       '- sleep_quality 主观睡眠质量 1-5 (1=很差,5=很好)',
       '- night_interrupted 夜里是否被弄醒 true/false',
       '- energy 此刻精力 1-5 (1=很困,5=很精神)',
       '输出 {"sleep_quality":int|null,"night_interrupted":bool|null,"energy":int|null}'].join('\n')
    : ['这是【睡前自检】回复。codebook：',
       '- day_stress 当天整体压力 1-5',
       '- day_mood 当天情绪：原话里的那个词（如"烦躁"/"平静"），没说=null',
       '- alcohol 当天总饮酒量：none | 1-2 | 3+',
       '- caffeine 末次咖啡因 none|morning|afternoon|evening',
       '- exercise 剧烈运动时段 none|morning|afternoon|within_3h_sleep',
       '- anomaly_note 异常一句话(生病/熬夜补觉/倒时差/出差/大事)，明确说"没有/都没有"=null',
       '输出 {"day_stress":int|null,"day_mood":str|null,"alcohol":str|null,"caffeine":str|null,"exercise":str|null,"anomaly_note":str|null}'].join('\n');
  return [{ role: 'system', content: `${head}\n\n${book}` }, { role: 'user', content: rawText }];
}

async function callLLM(messages) {
  const url = (ENV.LLM_BASE_URL || 'https://api.deepseek.com/v1').replace(/\/$/, '') + '/chat/completions';
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(new Error('timeout')), parseInt(ENV.LLM_TIMEOUT_MS || '30000', 10));
  try {
    const r = await fetch(url, {
      method: 'POST', signal: ac.signal,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${ENV.LLM_API_KEY}` },
      body: JSON.stringify({ model: ENV.LLM_MODEL || 'deepseek-chat', temperature: 0.2, response_format: { type: 'json_object' }, messages }),
    });
    if (!r.ok) throw new Error(`LLM ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return JSON.parse((await r.json()).choices[0].message.content);
  } finally { clearTimeout(timer); }
}

function mockCode(promptId, raw) {
  if (promptId === 'morning_anchor') return {
    sleep_quality: /不错|还行|挺好|好/.test(raw) ? 4 : /差|没睡好|糟/.test(raw) ? 2 : 3,
    night_interrupted: /醒|吵|起夜|打断/.test(raw) ? true : (/没.*醒|一觉到天亮/.test(raw) ? false : null),
    energy: /困|累|乏/.test(raw) ? 2 : /精神|有劲|清醒/.test(raw) ? 4 : null,
  };
  return {
    day_stress: /压力大|很累|崩|烦/.test(raw) ? 4 : /轻松|还好/.test(raw) ? 2 : 3,
    day_mood: (raw.match(/情绪[：: ]*([^\s，。,.]{1,4})/) || [])[1] || (/烦躁|焦虑|平静|开心|低落|疲惫/.exec(raw) || [])[0] || null,
    alcohol: /两瓶|啤酒|1-2|喝了点/.test(raw) ? '1-2' : /没喝|不喝|无酒/.test(raw) ? 'none' : null,
    caffeine: /下午.*咖啡|咖啡.*下午/.test(raw) ? 'afternoon' : /没.*咖啡/.test(raw) ? 'none' : null,
    exercise: /没运动|没动/.test(raw) ? 'none' : null,
    anomaly_note: null,
  };
}

export async function codeReply(db, rawId) {
  const r = db.prepare('SELECT * FROM esm_raw WHERE id=?').get(rawId);
  if (!r) throw new Error(`esm_raw ${rawId} 不存在`);
  if (r.skipped || !r.raw_text) return { rawId, coded: [], note: 'skipped/空' };
  if (!r.prompt_id.endsWith('_anchor')) return { rawId, coded: [], note: '追问回复不进固定 codebook' };

  const out = USE_MOCK ? mockCode(r.prompt_id, r.raw_text) : await callLLM(coderMessages(r.prompt_id, r.raw_text));
  const model = USE_MOCK ? 'mock' : (ENV.LLM_MODEL || 'deepseek-chat');
  const at = cstIso();

  const rows = [];
  // 写入做成一个事务：DELETE+重插 esm_coded 原子替换（避免崩在两句之间留空洞），daily_events 同事务。
  tx(() => {
    db.prepare('DELETE FROM esm_coded WHERE raw_id=?').run(rawId); // 幂等重跑
    const ins = db.prepare('INSERT INTO esm_coded (raw_id,construct,value_num,value_text,model,coded_at) VALUES (?,?,?,?,?,?)');
    const addNum = (c, v) => { if (v != null) { ins.run(rawId, c, v, null, model, at); rows.push({ c, num: v }); } };
    const addText = (c, v) => { if (v != null && String(v).trim()) { ins.run(rawId, c, null, String(v).trim(), model, at); rows.push({ c, text: v }); } };

    if (r.prompt_id === 'morning_anchor') {
      addNum('sleep_quality', clampScore(out.sleep_quality));
      addNum('night_interrupted', asBool01(out.night_interrupted));
      addNum('energy', clampScore(out.energy));
    } else {
      addNum('day_stress', clampScore(out.day_stress));
      addText('day_mood', out.day_mood);
      const date = r.ts_local.slice(0, 10);
      // COALESCE 保护（rank12）：本次抽不到(null)的字段保留旧值——避免一次抖动重编码把已确定的真值
      // （如"下午喝过咖啡"）清成 null，导致健康解读误判。只有这次真抽到新值才覆盖。
      db.prepare(`INSERT INTO daily_events (date,alcohol,caffeine,exercise,anomaly_note,raw_id,coded_at)
                  VALUES (?,?,?,?,?,?,?)
                  ON CONFLICT(date) DO UPDATE SET
                    alcohol=COALESCE(excluded.alcohol, daily_events.alcohol),
                    caffeine=COALESCE(excluded.caffeine, daily_events.caffeine),
                    exercise=COALESCE(excluded.exercise, daily_events.exercise),
                    anomaly_note=COALESCE(excluded.anomaly_note, daily_events.anomaly_note),
                    raw_id=excluded.raw_id, coded_at=excluded.coded_at`)
        .run(date, inEnum(out.alcohol, 'alcohol'), inEnum(out.caffeine, 'caffeine'),
             inEnum(out.exercise, 'exercise'), out.anomaly_note?.trim() || null, rawId, at);
    }
  });
  return { rawId, model, coded: rows };
}

// ============ 追问（只问不评·结构护栏）============
const _PRESCRIPTIVE = ['你应该', '你必须', '建议你去', '赶紧', '一定要'];
const _CAUSAL = ['因为', '导致', '所以你会', '使得', '会引起', '造成', 'predict', 'cause'];
const ASSESS_WORDS = [..._PRESCRIPTIVE, ..._CAUSAL, '建议', '解读', '说明你', '其实是', '分数', '评分', '打分', '不如', '最好', '记得要', '注意'];

export function sanitizeFollowup(q) {
  const t = (q ?? '').trim();
  if (!t) return null;
  if (!/[？?]/.test(t)) return null;                        // 必须是问句
  if (ASSESS_WORDS.some((w) => t.includes(w))) return null; // 命中评价/解读/建议 → 丢
  return t;
}

function followupMessages(promptId, rawText) {
  const sys = [
    `你是${IDENTITY.ownerName}的采集助手，沿用"小王"的口吻：温暖、短句、像朋友。`,
    '你只有一个能力：在他刚答完晨/晚自检后，至多追问【一个】问题——把原话里值得多记一笔的点挖深(纵向)，或补一个分析会用到的相邻情境(横向)。',
    '铁律（系统会做结构校验，违反则你的输出被直接丢弃）：',
    '1) 你只能【提问】，绝不评价、解读、下结论、给建议、给分数、给安慰式总结。',
    '2) 一句话、口语、带"不想说就跳过"的轻松感。',
    '3) 他要是答得很平淡、或明确说没事，就返回 null——别为追问而追问。',
    '输出严格 JSON：{"question": "……？" 或 null}',
  ].join('\n');
  const tag = promptId === 'morning_anchor' ? '晨起' : '晚间';
  return [{ role: 'system', content: sys }, { role: 'user', content: `他刚答的是【${tag}】，原话："${rawText}"。要不要追问一句？` }];
}

function mockFollowup(promptId, raw) {
  if (/都没有|都挺好|没事|平淡|正常|没被/.test(raw)) return null;
  if (/吵醒|醒|没睡好/.test(raw)) return '那被吵醒之后是很快又睡着了，还是翻来覆去了一阵？（不想说就跳过～）';
  if (/压力|烦|累|崩/.test(raw)) return '今天主要是什么事压着你呀？一句话就行，不想聊也没关系。';
  return null;
}

export async function followupQuestion(promptId, rawText) {
  if (!rawText?.trim()) return { question: null };
  try {
    const out = USE_MOCK ? { question: mockFollowup(promptId, rawText) } : await callLLM(followupMessages(promptId, rawText));
    return { question: sanitizeFollowup(out?.question) };
  } catch { return { question: null }; }
}

// ============ v2 胶水：pending 状态 + 入站拦截 + 排程 ============
const PENDKEY = 'pending';
function getState(db, k) { const r = db.prepare('SELECT v FROM bot_state WHERE k=?').get(k); return r ? r.v : null; }
function setState(db, k, v) {
  db.prepare('INSERT INTO bot_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v').run(k, v == null ? null : String(v));
}
export function getPending(db = getDb()) { const v = getState(db, PENDKEY); return v ? JSON.parse(v) : null; }
// setPending 记录 setAt 时间戳，供 TTL 判定（到点设 pending 但用户隔很久才发话时不再吞成打卡回复）。
export function setPending(db, p) { setState(db, PENDKEY, p ? JSON.stringify({ ...p, setAt: nowMs() }) : null); }

// pending 存活窗口（从 setAt 起算）。超窗的 pending 视为"用户没在回打卡"，不拦截、交给 agent（审计 rank3）。
const PENDING_TTL_MS = {
  morning_anchor: 5 * 3600 * 1000,    // 晨 08:30 → ~13:30
  evening_anchor: 9 * 3600 * 1000,    // 晚 22:30 → 次日 ~07:30
  morning_followup: 60 * 60 * 1000,   // 追问 1h
  evening_followup: 60 * 60 * 1000,
};
export function isPendingExpired(pend) {
  if (!pend || !pend.setAt) return false; // 无 setAt（旧库/手动设）按不过期，向后兼容（不误伤线上已存在的 pending）
  const ttl = PENDING_TTL_MS[pend.type] || 4 * 3600 * 1000;
  return (nowMs() - pend.setAt) > ttl;
}

// =====================================================================
// recordCheckin —— 把"用户这条原话是打卡回复"登记进采集库（不可逆层）。
//
// 原则11 落地：是否"在回打卡"这件【模糊判断】交给模型（它调 record_checkin 工具即代表它判断"是"）；
// 本函数只做【确定性】的登记 + 中性回执 + 触发编码。原话以 harness 持有的真实输入为准（调用方传入），
// 不信任模型对原话的复述（防改写污染不可逆层）。红线"只问不评"由 loop 短路 + 固定回执 + sanitizeFollowup 三重守。
//
// 返回 { ok, reply?, error? }。reply 是要发给子淇的固定中性文案（loop 用它覆盖模型本轮自由发挥）。
// =====================================================================
export async function recordCheckin(db, rawText) {
  const pend = getPending(db);
  if (!pend || !/_anchor$|_followup$/.test(pend.type || '')) {
    return { ok: false, error: '当前没有待回的晨/晚打卡，不应调用 record_checkin' };
  }
  const raw = String(rawText || '').trim();
  if (!raw) return { ok: false, error: 'raw_text（用户原话）不能为空' };

  if (pend.type.endsWith('_anchor')) {
    const rawId = capture(db, { promptId: pend.type, rawText: raw, ts: cstIso() }); // 不可逆层先行
    setPending(db, null);
    // 编码异步触发（可再生层），失败不影响登记（原话已落 esm_raw）。
    codeReply(db, rawId).catch((e) => console.error('[esm] codeReply 失败(忽略): %s', e.message));
    const fq = await followupQuestion(pend.type, raw); // 至多一个中性追问（sanitizeFollowup 守红线）
    if (fq.question) {
      setPending(db, { type: pend.type === 'morning_anchor' ? 'morning_followup' : 'evening_followup', anchorRawId: rawId });
      return { ok: true, reply: fq.question };
    }
    return { ok: true, reply: '记下了 📝' };
  }
  // followup 回复
  capture(db, { promptId: pend.type, rawText: raw, ts: cstIso(), parentId: pend.anchorRawId });
  setPending(db, null);
  return { ok: true, reply: '好，记下了 🙏' };
}

function safeEpisode(db, args) {
  try { appendEpisode(args); } catch (e) { console.error('[esm] appendEpisode 失败(忽略): %s', e.message); }
}

/**
 * ESM 入站 fast-path（agent 之前）。返回 { handled, replies? }。
 *
 * 原则11：不再前置确定性吞"打卡回复"——"这条是不是在回打卡"是【模糊判断】，交给模型
 * （context 注入提示 → 模型调 record_checkin → loop 短路守红线）。这里只保留【显式语法】fast-path：
 *  - # 开头 → 原话落黑匣子 notes(权威正本) + episodes 副本(召回)；空# 不处理交 agent；handled=true。
 *  - 其它（含 pending 在时的打卡回复）→ handled=false，一律交给 agent。
 */
export async function handleEsmInbound(db, text, sessionId = null) {
  const t = (text || '').trim();
  if (!t) return { handled: false };

  // pending 过期保护：到点设了 pending 但用户隔很久才发话 → 清掉，避免 context 还在提示"待回打卡"。
  const pend = getPending(db);
  if (pend && isPendingExpired(pend)) {
    console.error('[esm] pending(%s) 过期(age %dmin)，清除', pend.type, Math.round((nowMs() - pend.setAt) / 60000));
    setPending(db, null);
  }

  // # 快记事件（显式语法，无歧义，保留为确定性 fast-path）。
  if (t.startsWith('#')) {
    const body = t.slice(1).trim();        // 去掉前导 # 与空白（# 是语法不是内容）
    if (!body) return { handled: false };  // 空 #（光一个 # 或 "# "）→ 不污染黑匣子，交给 agent
    // 黑匣子先行（权威正本，绝不能丢）：appendNote 成功才算记下。
    try {
      appendNote({ sessionId, content: body, source: 'hashtag' });
    } catch (e) {
      // 失败要响、不假报成功（修旧的：safeEpisode 吞错后无条件回"记下了✓"=假成功 bug）。
      console.error('[esm] appendNote 失败: %s', e.message);
      return { handled: true, replies: ['没存上，再发一次。'] };
    }
    // episodes 副本（召回索引，best-effort）：带 sessionId 才能被 session-scoped 召回（修 NULL-session 召不回）。
    safeEpisode(db, { sessionId, role: 'user', content: body, entity: 'event' });
    return { handled: true, replies: ['记下了 ✓'] };
  }

  // 其它一律交给 agent（含"在回打卡"——由模型经 record_checkin 登记，红线由 loop 短路守）。
  return { handled: false };
}

/**
 * 排程检查（main worker tick 每拍调用，便宜）。到点返回要发的提示，并打"今天已发"标 + 设 pending。
 * 返回 { kind:'prompt'|'review', content } 或 null。发送由 main 经 outbox 完成（本模块不发）。
 */
// 'HH:MM' → 当天分钟数（用于"到点/已过点但未超补发上限"的窗口判定）。
function hmToMin(hm) { const [h, m] = hm.split(':').map(Number); return h * 60 + m; }
// 补发上限：到点后最多迟这么多分钟内仍补发；超了视为这天错过（不在奇怪的钟点发，如晚上 11 点补晨问候）。
const MAX_CATCHUP_LATE_MIN = 180;

// 排程检查（main worker tick 每拍调用，便宜，【纯读不写】）。
// 修复①（rank8 missed-minute）：不再要求 tick 恰好命中目标分钟——到点起、到点后 MAX_CATCHUP_LATE_MIN 分钟内
//   当天未发就补发一次（进程重启/卡顿跨过那一分钟也不漏当天），又不会在离谱的钟点补发。
// 修复②（fresh-correctness 静默丢失）：不在这里置"已发"标志/pending，改由 main 在 enqueue 成功后调 markEsmSent
//   落标——否则 enqueue 失败但标志已置 → 当天提示永久丢失。幂等仍由 sent:tag:date 标志 + outbox dedupHash(含 date) 双重保证。
export function esmDuePrompt(db = getDb()) {
  const { hm, date, dow } = cstParts();
  const nowMin = hmToMin(hm);
  for (const [time, type, tag] of [[MORNING, 'morning_anchor', 'm'], [EVENING, 'evening_anchor', 'e']]) {
    const late = nowMin - hmToMin(time);
    if (late >= 0 && late <= MAX_CATCHUP_LATE_MIN && getState(db, `sent:${tag}:${date}`) !== '1') {
      return { kind: 'prompt', type, tag, date, content: PROMPTS[type], dedupTag: `esm:${tag}:${date}` };
    }
  }
  const reviewLate = nowMin - hmToMin(REVIEW);
  if (dow === 0 && reviewLate >= 0 && reviewLate <= MAX_CATCHUP_LATE_MIN && getState(db, `sent:review:${date}`) !== '1') {
    return { kind: 'review', tag: 'review', date, content: weeklyReviewText(db), dedupTag: `esm:review:${date}` };
  }
  return null;
}

// 提示成功入队【之后】才落"已发"标志 + 设 pending（与 enqueue 解耦，保证"标了已发但其实没发出去"不会发生）。
export function markEsmSent(db, due) {
  if (!due || !due.tag || !due.date) return;
  setState(db, `sent:${due.tag}:${due.date}`, '1');
  if (due.kind === 'prompt' && due.type) setPending(db, { type: due.type });
}

// 周回顾：过去 7 天晨/晚锚点完整度。中性陈述、无 streak/解读/建议（守红线查看端）。
function weekStats(db) {
  const today = cstShift();
  const days = [];
  for (let i = 6; i >= 0; i--) days.push(new Date(today.getTime() - i * 86400000).toISOString().slice(0, 10));
  const rows = db.prepare(
    "SELECT substr(ts_local,1,10) d, prompt_id p FROM esm_raw WHERE prompt_id IN ('morning_anchor','evening_anchor') AND substr(ts_local,1,10) >= ? AND skipped=0"
  ).all(days[0]);
  const m = new Set(), e = new Set();
  for (const r of rows) (r.p === 'morning_anchor' ? m : e).add(r.d);
  return { mornings: m.size, evenings: e.size, missing: days.filter((d) => !m.has(d) && !e.has(d)) };
}
export function weeklyReviewText(db = getDb()) {
  const { mornings, evenings, missing } = weekStats(db);
  const lines = ['这周回顾 📋', `晨 ${mornings} 天 / 晚 ${evenings} 天`];
  if (missing.length) lines.push(`这几天空着：${missing.map((d) => d.slice(5)).join('、')}`);
  lines.push('想补就发当天原话，不补也行；这周有啥想记下的也随时发。');
  return lines.join('\n');
}

// ============ --selftest（离线 mock，临时 db） ============
async function runSelftest() {
  const { mkdtempSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const db = await import('./db.mjs');
  let pass = 0, fail = 0;
  const ok = (c, m) => { if (c) { pass++; console.log('  ✓ ' + m); } else { fail++; console.log('  ✗ ' + m); } };

  const dir = mkdtempSync(join(tmpdir(), 'xw2-esm-'));
  process.env.XW2_DB_PATH = join(dir, 'v2.db');
  try {
    const conn = db.initDb(process.env.XW2_DB_PATH);
    initEsmSchema(conn);

    // 原则11：pending 在时，handleEsmInbound 不再吞打卡回复——交给 agent（模型经 record_checkin 登记）。
    setPending(conn, { type: 'morning_anchor' });
    const passAnchor = await handleEsmInbound(conn, '睡得还行，凌晨被楼上吵醒一次，现在还挺困');
    ok(passAnchor.handled === false, '原则11：晨锚 pending 在时打卡回复不被前置吞、交给 agent（吞命令根因已除）');

    // record_checkin（模型判断"在回打卡"后调）做确定性登记 + 中性回执
    const rc1 = await recordCheckin(conn, '睡得还行，凌晨被楼上吵醒一次，现在还挺困');
    ok(rc1.ok && rc1.reply, 'recordCheckin 登记成功 + 给中性回执');
    const raw = conn.prepare("SELECT * FROM esm_raw WHERE prompt_id='morning_anchor' ORDER BY id DESC LIMIT 1").get();
    ok(raw && raw.raw_text.includes('吵醒'), '原话逐字入 esm_raw（不可逆层）');
    await new Promise((r) => setTimeout(r, 100)); // 等 fire-and-forget 编码（mock 即时）
    const coded = conn.prepare('SELECT construct,value_num FROM esm_coded WHERE raw_id=?').all(raw.id);
    ok(coded.some((c) => c.construct === 'night_interrupted' && c.value_num === 1), 'mock 编码：被吵醒→night_interrupted=1');

    // recordCheckin 无 pending 时拒绝（防模型在没打卡时乱登记）
    setPending(conn, null);
    const rcNo = await recordCheckin(conn, '随便一句');
    ok(rcNo.ok === false, '无 pending 时 recordCheckin 拒绝（结构守"只在真打卡时登记"）');

    // 平淡晚锚：recordCheckin 中性确认，无解读（红线）
    setPending(conn, { type: 'evening_anchor' });
    const rc2 = await recordCheckin(conn, '都挺好的，没什么特别的');
    ok(rc2.ok && /记下了/.test(rc2.reply) && !/建议|解读|分数|应该/.test(rc2.reply), '平淡晚锚 recordCheckin→中性确认，不给解读（红线）');

    // # 快记：显式语法 fast-path → 黑匣子 notes(正本) + episodes 副本(召回)
    const r3 = await handleEsmInbound(conn, '#中午和老王吃饭聊了项目', 'wecom:ziqi');
    ok(r3.handled && r3.replies[0] === '记下了 ✓', '# 快记→中性确认（显式语法保留为 fast-path）');
    const ev = conn.prepare("SELECT count(*) c FROM episodes WHERE entity='event'").get();
    ok(ev.c === 1, '# 快记落 event episode（召回副本）');
    const noteRow = conn.prepare("SELECT content, session_id FROM notes ORDER BY id DESC LIMIT 1").get();
    ok(noteRow && noteRow.content === '中午和老王吃饭聊了项目', '# 快记原话落黑匣子 notes（去掉#、权威正本）');
    ok(noteRow && noteRow.session_id === 'wecom:ziqi', '# 快记 notes 带 sessionId');
    const evRow = conn.prepare("SELECT content, session_id FROM episodes WHERE entity='event' ORDER BY id DESC LIMIT 1").get();
    ok(evRow && evRow.content === '中午和老王吃饭聊了项目' && evRow.session_id === 'wecom:ziqi',
       '# 快记 episodes 副本：去#、带 sessionId（修 NULL-session 召不回 bug）');
    // 空 # 守卫：光一个 # 或 "# " 不写黑匣子、交给 agent
    const notesBefore = conn.prepare("SELECT count(*) c FROM notes").get().c;
    const rEmpty = await handleEsmInbound(conn, '#   ', 'wecom:ziqi');
    ok(rEmpty.handled === false, '空 #（# 后无内容）不拦截、交给 agent');
    ok(conn.prepare("SELECT count(*) c FROM notes").get().c === notesBefore, '空 # 不往黑匣子写垃圾行');

    // 自由消息 → 不拦截，交 agent
    setPending(conn, null);
    const r4 = await handleEsmInbound(conn, '帮我看下今天日程');
    ok(r4.handled === false, '自由消息不被 ESM 拦截（交给 agent）');

    // sanitizeFollowup 结构护栏
    ok(sanitizeFollowup('你应该早点睡，建议少喝咖啡。') === null, '评价式追问被丢');
    ok(sanitizeFollowup('你今天就是太累了。') === null, '陈述句被丢');
    ok(sanitizeFollowup('那今天还撑得住吗？') === '那今天还撑得住吗？', '干净问句放行');

    // 周回顾：中性、无 streak
    const wr = weeklyReviewText(conn);
    ok(/回顾/.test(wr) && !/连续|streak|完成率|应该|建议|解读/.test(wr), '周回顾中性、无游戏化');

    // --- pending TTL：过期 pending 不吞消息（审计 rank3 串话/吞请求/污染数据） ---
    setPending(conn, { type: 'morning_anchor' });
    conn.prepare(`UPDATE bot_state SET v=? WHERE k='pending'`).run(JSON.stringify({ type: 'morning_anchor', setAt: nowMs() - 6 * 3600 * 1000 })); // 人为超 5h 窗
    const rExpired = await handleEsmInbound(conn, '帮我查下今天日程');
    ok(rExpired.handled === false, '过期 pending 不吞消息、交给 agent（修 rank3：早上第一句被吞）');
    ok(getPending(conn) === null, '过期 pending 被清除');

    // --- esmDuePrompt: hm>=time 补发 + markEsmSent 落标解耦（修 missed-minute + 静默丢失） ---
    db.__setClockForTest(() => Date.parse('2026-06-25T00:45:00Z')); // 08:45 CST：已过 08:30、当天未发
    const due1 = esmDuePrompt(conn);
    ok(due1 && due1.type === 'morning_anchor' && due1.tag === 'm' && due1.date === '2026-06-25',
       'hm>=time：08:45 仍补发当天未发的晨锚（返回 tag/date 供 markEsmSent）');
    ok(getState(conn, 'sent:m:2026-06-25') !== '1', 'esmDuePrompt 纯读：未提前置已发标志（标在 enqueue 成功后）');
    markEsmSent(conn, due1);
    ok(getState(conn, 'sent:m:2026-06-25') === '1', 'markEsmSent 落"已发"标志');
    ok(getPending(conn) && getPending(conn).type === 'morning_anchor', 'markEsmSent 设 pending 等回复');
    ok(esmDuePrompt(conn) === null, 'markEsmSent 后同日晨锚不再触发（标志幂等）');
    db.__setClockForTest(() => Date.parse('2026-06-26T05:00:00Z')); // 次日 13:00 CST：超补发上限 180min
    const dueLate = esmDuePrompt(conn);
    ok(!dueLate || dueLate.type !== 'morning_anchor', '超补发上限(180min)不再补发晨锚（不在离谱钟点发）');
    db.__setClockForTest(null);

    // --- daily_events COALESCE：二次编码缺值不清旧值（rank12 数据回退防护） ---
    setPending(conn, { type: 'evening_anchor' });
    await recordCheckin(conn, '压力还行，下午喝了咖啡');
    await new Promise((r) => setTimeout(r, 100)); // 等 fire-and-forget 编码
    const evDay = conn.prepare("SELECT ts_local FROM esm_raw WHERE prompt_id='evening_anchor' ORDER BY id DESC LIMIT 1").get().ts_local.slice(0, 10);
    ok(conn.prepare('SELECT caffeine FROM daily_events WHERE date=?').get(evDay)?.caffeine === 'afternoon', '首次编码 caffeine=afternoon');
    setPending(conn, { type: 'evening_anchor' });
    await recordCheckin(conn, '今天都挺好的，没什么特别的'); // 不提咖啡 → mock caffeine=null
    await new Promise((r) => setTimeout(r, 100));
    ok(conn.prepare('SELECT caffeine FROM daily_events WHERE date=?').get(evDay)?.caffeine === 'afternoon', 'COALESCE：第二条没提咖啡，旧 caffeine 未被清成 null');
  } catch (e) {
    fail++;
    console.log('  ✗ selftest 异常: ' + e.stack);
  } finally {
    try { (await import('./db.mjs')).__closeForTest(); } catch {}
    rmSync(dir, { recursive: true, force: true });
  }
  console.log(`\n[esm.mjs selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

if (process.argv.includes('--selftest') && import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  await runSelftest();
}
