// =====================================================================
// memory.mjs —— 小王 v2 分层记忆层（C 分层记忆）
//
// 职责（单一）：episodes 只追加 + facts 半自动经 staging + retrieve top-k 召回。
// 依赖方向（单向无环）：db ← memory。本模块只通过 getDb()/tx()/nowMs() 拿能力，
// 绝不自己 new DatabaseSync（单写连接铁律），绝不 import 上层（tools/loop/main）。
//
// 召回路径由 db 的 MEMORY_MODE 决定：
//   'fts5' → episodes_fts BM25 × exp(-Δdays/τ) recency × entity boost，综合排序取 top-k
//   'like' → content LIKE '%query%'（参数化）+ entity 命中加权 + ORDER BY ts DESC LIMIT k
// 注入永远 top-k，绝不整表塞（团队原则 8：上下文是最稀缺资源）。
//
// 首版纪律（照任务书）：不自动抽取 facts、不做 Sawtooth 压缩、episodes 全留不删。
// memory_write 工具落点是 facts_staging（半自动，等人工确认），不直接写 facts。
// =====================================================================

import { getDb, tx, nowMs } from './db.mjs';
// db.mjs 把召回模式导出为 getMemoryMode()（契约：保证 builder 拿到最终值而非初始 undefined）。
// 用命名空间 import 兜底：无论 db 暴露的是 getMemoryMode() 还是裸 MEMORY_MODE 都能读到，
// 且即便 db 尚未就绪也不会因缺失 binding 崩模块（失败响亮但不静默炸）。
import * as db from './db.mjs';

// ---- 常量：与 db.mjs/main.mjs 顶部集中常量对齐，禁止魔法数散落 ----
// recency 半衰参数：契约 RECENCY_TAU_DAYS=14。优先读 db 导出的同名常量保持单一真源。
const RECENCY_TAU_DAYS =
  typeof db.RECENCY_TAU_DAYS === 'number' ? db.RECENCY_TAU_DAYS : 14;
const MS_PER_DAY = 86400000;
// entity 命中给召回加成：命中目标 entity 的 episode 排序权重乘以这个系数。
// 取值原因：1.5 让"主题相关"明显上浮但不至于碾压 BM25/recency 信号，避免单一维度独裁。
const ENTITY_BOOST = 1.5;

// 读当前召回模式。为什么包一层：db.mjs 在 initDb() 里 try/catch 建 FTS5 后才定下 MEMORY_MODE，
// 这里每次调用都重新读，保证拿到的是"建表后"的最终值，不缓存初始 undefined。
function memoryMode() {
  if (typeof db.getMemoryMode === 'function') return db.getMemoryMode();
  if (typeof db.MEMORY_MODE === 'string') return db.MEMORY_MODE;
  // 拿不到则保守降级到 LIKE：宁可慢而稳，也不要因 FTS5 假设崩召回（失败要响）。
  console.warn('[memory] MEMORY_MODE unreadable from db.mjs, defaulting to LIKE');
  return 'like';
}

// =====================================================================
// 1. episodes：只追加对话/事件流
// =====================================================================

/**
 * 追加一条 episode（只追加，永不改写——这是记忆可信的基础）。
 * tx() 内单条 INSERT；FTS5 模式下 db 的触发器自动同步 episodes_fts，本函数无需手动写虚表。
 * 返回新行 id。
 *
 * @param {{ts?:number, sessionId?:string|null, role:string, content:string,
 *           entity?:string|null, taskId?:number|null}} ep
 * @returns {number} 新 episode 的 id
 */
export function appendEpisode({
  ts = nowMs(),
  sessionId = null,
  role,
  content,
  entity = null,
  taskId = null,
} = {}) {
  // role/content 是 schema NOT NULL 字段，缺了就是上游 bug——早炸早暴露，别静默吞。
  if (!role) throw new Error('[memory] appendEpisode: role is required');
  if (content === undefined || content === null) {
    throw new Error('[memory] appendEpisode: content is required');
  }

  return tx((conn) => {
    const stmt = conn.prepare(
      `INSERT INTO episodes (ts, session_id, role, content, entity, task_id)
       VALUES (?, ?, ?, ?, ?, ?)`,
    );
    const info = stmt.run(
      ts,
      sessionId,
      String(role),
      String(content),
      entity,
      taskId,
    );
    // node:sqlite 的 run() 返回 { lastInsertRowid, changes }
    return Number(info.lastInsertRowid);
  });
}

// =====================================================================
// 2. retrieve：top-k 召回（按 MEMORY_MODE 分流）
// =====================================================================

/**
 * 召回与 query 最相关的 top-k episodes。
 * - 'fts5'：episodes_fts MATCH(query) 取 BM25，再乘 recency=exp(-Δdays/τ) 和 entity boost，综合排序。
 * - 'like'：content LIKE '%query%'（参数化）+ entity 命中加权，ORDER BY ts DESC LIMIT k。
 * - query 为空：直接返回最近 k 条（无检索语义，纯近况）。
 * 永远 top-k，绝不返整表（原则 8）。返回带 score，供上层调试/二次排序。
 *
 * @param {string} query
 * @param {number} [k=8]
 * @param {{entity?:string|null}} [opts]  可选：指定 entity 做 boost（不传则从 query 不做 entity 加权）
 * @returns {Array<{id,ts,role,content,entity,task_id,score}>}
 */
export function retrieve(query, k = 8, opts = {}) {
  const limit = Math.max(1, k | 0);
  const q = (query ?? '').trim();
  const entity = opts.entity ?? null;
  // sessionId 下推到 SQL WHERE（不是 post-filter），保证多源/跨上下文不错召回（消化审计 memory.mjs:103 缺口）。
  const sessionId = opts.sessionId ?? null;
  const conn = getDb();

  // 空 query：退化为"最近 k 条"。这是召回的合理兜底——没检索词时近况最相关。
  if (q === '') {
    return recentEpisodes(conn, limit, sessionId);
  }

  const mode = memoryMode();
  if (mode === 'fts5') {
    try {
      const ftsHits = retrieveFts(conn, q, limit, entity, sessionId);
      // CJK 兜底：unicode61 分词器把整段中文当一个 token，子串（如 "手环"）召不回，
      // 返回 0 命中。此时 LIKE 仍能子串匹配——FTS 空手而归就回退 LIKE，保证中文召回不丢。
      // （英文/有命中场景维持 FTS 的 BM25×recency 排序，不受影响。）
      if (ftsHits.length > 0) return ftsHits;
      return retrieveLike(conn, q, limit, entity, sessionId);
    } catch (e) {
      // FTS5 运行时异常（如 MATCH 语法被特殊字符破坏）不应整链崩——降级 LIKE 兜底，但留证据。
      console.warn(
        '[memory] FTS5 retrieve failed, falling back to LIKE: %s',
        e.message,
      );
      return retrieveLike(conn, q, limit, entity, sessionId);
    }
  }
  return retrieveLike(conn, q, limit, entity);
}

/** 最近 k 条 episodes（空 query 兜底 / 召回降级的公共近况源）。sessionId 非空则只取该会话。 */
function recentEpisodes(conn, limit, sessionId = null) {
  const where = sessionId ? 'WHERE session_id = ?' : '';
  const params = sessionId ? [sessionId, limit] : [limit];
  const rows = conn
    .prepare(
      `SELECT id, ts, role, content, entity, task_id
       FROM episodes
       ${where}
       ORDER BY ts DESC
       LIMIT ?`,
    )
    .all(...params);
  // score 给个随时间衰减的占位值，保持返回结构一致，便于上层统一处理。
  const now = nowMs();
  return rows.map((r) => ({
    ...r,
    score: recencyWeight(r.ts, now),
  }));
}

/**
 * FTS5 召回：先用 BM25 取候选，再用 recency × entity boost 重排。
 * 为什么取多于 k 的候选再重排：BM25 only 排序不含时间/主题信号，
 * 多召一批（k*4，封顶 64）让 recency/entity 有重排空间，再砍到 top-k。
 */
function retrieveFts(conn, q, limit, entity, sessionId = null) {
  const matchExpr = toFtsMatch(q);
  if (matchExpr === '') {
    // 规范化后没有任何可检索 token（纯标点）→ 退化为近况，避免 MATCH '' 报错。
    return recentEpisodes(conn, limit, sessionId);
  }

  const candidateN = Math.min(limit * 4, 64);
  // bm25(episodes_fts) 越小越相关（rank 升序）；取负转成"越大越好"的 relevance。
  const sessFilter = sessionId ? ' AND e.session_id = ?' : '';
  const params = sessionId ? [matchExpr, sessionId, candidateN] : [matchExpr, candidateN];
  const rows = conn
    .prepare(
      `SELECT e.id, e.ts, e.role, e.content, e.entity, e.task_id,
              bm25(episodes_fts) AS bm25_rank
       FROM episodes_fts
       JOIN episodes e ON e.id = episodes_fts.rowid
       WHERE episodes_fts MATCH ?${sessFilter}
       ORDER BY bm25_rank ASC
       LIMIT ?`,
    )
    .all(...params);

  const now = nowMs();
  const scored = rows.map((r) => {
    // 把 bm25_rank（越小越好，可能为负）转成正向相关度：1/(1+rank-min) 形式不稳定，
    // 直接用 exp(-rank) 之类又易溢出。这里用单调变换 relevance = 1/(1 + max(0, bm25_rank))
    // 配合 SQLite bm25 默认输出（非负、越小越相关）。
    const relevance = 1 / (1 + Math.max(0, r.bm25_rank));
    const recency = recencyWeight(r.ts, now);
    const boost = entity && r.entity && r.entity === entity ? ENTITY_BOOST : 1;
    const score = relevance * recency * boost;
    // 不把内部 bm25_rank 泄露给上层，保持返回结构干净。
    const { bm25_rank, ...rest } = r;
    return { ...rest, score };
  });

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, limit);
}

/**
 * LIKE 降级召回：content LIKE '%query%'（参数化，防注入）+ entity 命中加权 + ts 近优先。
 * 多召候选再按 (entity 命中, ts) 重排，砍到 top-k；保证降级路径也有 entity boost 语义。
 */
function retrieveLike(conn, q, limit, entity, sessionId = null) {
  const candidateN = Math.min(limit * 4, 64);
  // 参数化 LIKE：用占位符 + 拼好的 '%q%' 值，绝不字符串插值进 SQL（防 SQL 注入）。
  const likeVal = '%' + escapeLike(q) + '%';
  const sessFilter = sessionId ? ' AND session_id = ?' : '';
  const params = sessionId ? [likeVal, sessionId, candidateN] : [likeVal, candidateN];
  const rows = conn
    .prepare(
      `SELECT id, ts, role, content, entity, task_id
       FROM episodes
       WHERE content LIKE ? ESCAPE '\\'${sessFilter}
       ORDER BY ts DESC
       LIMIT ?`,
    )
    .all(...params);

  const now = nowMs();
  const scored = rows.map((r) => {
    const recency = recencyWeight(r.ts, now);
    const boost = entity && r.entity && r.entity === entity ? ENTITY_BOOST : 1;
    // LIKE 模式没有 BM25 相关度，用 recency × boost 作为 score（命中即相关，再按时间/主题排）。
    return { ...r, score: recency * boost };
  });

  // 主排序：entity 命中优先 → ts 新优先（契约：LIKE 走 entity + ORDER BY ts DESC）。
  scored.sort((a, b) => {
    const aHit = entity && a.entity === entity ? 1 : 0;
    const bHit = entity && b.entity === entity ? 1 : 0;
    if (aHit !== bHit) return bHit - aHit;
    return b.ts - a.ts;
  });
  return scored.slice(0, limit);
}

// ---- 召回辅助：纯函数，便于 selftest 单独验证 ----

/** recency 权重：exp(-Δdays/τ)。Δ<0（未来时间戳）夹到 0，权重封顶 1。 */
function recencyWeight(ts, now = nowMs()) {
  const deltaDays = Math.max(0, (now - ts) / MS_PER_DAY);
  return Math.exp(-deltaDays / RECENCY_TAU_DAYS);
}

/**
 * 把用户 query 规范化成 FTS5 MATCH 表达式。
 * 为什么需要：原始 query 里的标点/特殊符号会被 FTS5 当作 MATCH 语法（如 - " * ( ) :），
 * 破坏查询甚至抛错。这里抽出 token，每个 token 加双引号做短语量化，用 OR 连接，
 * 既避免语法注入又保留"任一词命中即召回"的宽松语义（召回宁宽勿漏，精排交给重排）。
 */
function toFtsMatch(q) {
  // 抽取连续的字母数字/CJK 片段作为 token（unicode61 分词器对应的可检索单元）。
  const tokens = (q.match(/[\p{L}\p{N}]+/gu) || [])
    .map((t) => t.trim())
    .filter((t) => t.length > 0)
    // 双引号包裹做短语，内部双引号转义（FTS5 短语里 "" 表示一个字面双引号）。
    .map((t) => '"' + t.replace(/"/g, '""') + '"');
  return tokens.join(' OR ');
}

/** 转义 LIKE 元字符（% _ 以及转义符本身），配合 ESCAPE '\\' 使用，防止用户输入被当通配符。 */
function escapeLike(s) {
  return String(s).replace(/[\\%_]/g, (ch) => '\\' + ch);
}

// =====================================================================
// 3. facts：半自动，经 staging（首版不自动抽取、不自动提升）
// =====================================================================

/**
 * memory_write 工具的底层落点：把一条候选 fact 写进 facts_staging（status='pending'）。
 * 首版不直接进 facts 表（半自动护栏：等人工确认后由运维/独立流程 promote）。
 * tx() 内单条 INSERT，返回 staging 行 id。
 *
 * @param {{entity?:string|null, fact:string, source?:'user_said'|'inferred'|'external'}} f
 * @returns {number} facts_staging 行 id
 */
export function stageFact({ entity = null, fact, source = 'inferred' } = {}) {
  if (!fact || String(fact).trim() === '') {
    throw new Error('[memory] stageFact: fact is required and non-empty');
  }
  // source 受 schema CHECK 约束；提前校验给出可读错误，而不是等 SQLite 抛 CHECK failed。
  if (!['user_said', 'inferred', 'external'].includes(source)) {
    throw new Error(
      `[memory] stageFact: invalid source '${source}' (must be user_said|inferred|external)`,
    );
  }

  return tx((conn) => {
    const stmt = conn.prepare(
      `INSERT INTO facts_staging (entity, fact, source, status, created_at)
       VALUES (?, ?, ?, 'pending', ?)`,
    );
    const info = stmt.run(entity, String(fact), source, nowMs());
    return Number(info.lastInsertRowid);
  });
}

/**
 * 把一条 staging fact 提升（promote）进正式 facts 表。
 * 首版"半自动"= 工具只能写 staging，promote 由人工/运维显式触发（非自动抽取）。
 * 提供此函数作为确认通道：accept 一条 staging → 写入 facts + 标记 staging 'accepted'，
 * 两步在同一 tx() 里原子完成（要么都成要么都不成，避免 staging 已 accepted 但 facts 没落库）。
 *
 * @param {number} stagingId  facts_staging.id
 * @param {{confidence?:number, importance?:number, validFrom?:number}} [opts]
 * @returns {number|null}  新 facts.id；若 staging 不存在或非 pending 返回 null（幂等，不重复 promote）
 */
export function promoteFact(stagingId, opts = {}) {
  const confidence = typeof opts.confidence === 'number' ? opts.confidence : 0.5;
  const importance = typeof opts.importance === 'number' ? opts.importance : 0.5;

  return tx((conn) => {
    const row = conn
      .prepare(`SELECT id, entity, fact, source, status FROM facts_staging WHERE id = ?`)
      .get(stagingId);

    // 不存在 / 已处理过 → 幂等返回 null，不报错也不重复 promote（半自动确认可能被重放）。
    if (!row || row.status !== 'pending') return null;

    const now = nowMs();
    const validFrom = typeof opts.validFrom === 'number' ? opts.validFrom : now;

    const ins = conn.prepare(
      `INSERT INTO facts
         (entity, fact, source, confidence, created_at, valid_from,
          superseded_by, importance, last_accessed, access_count)
       VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, 0)`,
    );
    const info = ins.run(
      row.entity ?? '', // staging.entity 可空，但 facts.entity NOT NULL → 兜底空串（同 pinFact，避免 promote 空 entity staging 时崩）
      row.fact,
      row.source,
      confidence,
      now,
      validFrom,
      importance,
    );
    const factId = Number(info.lastInsertRowid);

    // 标记 staging 已处理，避免再次 promote（同事务，保证一致）。
    conn
      .prepare(`UPDATE facts_staging SET status = 'accepted' WHERE id = ?`)
      .run(stagingId);

    return factId;
  });
}

/**
 * 取当前有效（未被 superseded）的 top-k facts，供 buildSystemPrompt 注入。
 * 有效 = superseded_by IS NULL。排序：importance 优先，其次 confidence（重要且可信的先注入）。
 * entity 非空则只取该 entity 的事实。永远 top-k，绝不整表注入（原则 8）。
 *
 * @param {string|null} [entity=null]
 * @param {number} [k=10]
 * @returns {Array<{entity,fact,confidence,importance}>}
 */
export function topFacts(entity = null, k = 10) {
  const limit = Math.max(1, k | 0);
  const conn = getDb();

  if (entity) {
    return conn
      .prepare(
        `SELECT entity, fact, confidence, importance
         FROM facts
         WHERE superseded_by IS NULL AND entity = ?
         ORDER BY importance DESC, confidence DESC
         LIMIT ?`,
      )
      .all(entity, limit);
  }
  return conn
    .prepare(
      `SELECT entity, fact, confidence, importance
       FROM facts
       WHERE superseded_by IS NULL
       ORDER BY importance DESC, confidence DESC
       LIMIT ?`,
    )
    .all(limit);
}

/**
 * pinFact —— 直接钉一条锚点事实（pinned=1），跳过 staging。
 *
 * 为什么单开一条路而不复用 stageFact/promoteFact：
 *   stageFact = 模型从对话里【推断】的事实 → 进 staging 等人工确认（半自动护栏，防模型瞎记污染）。
 *   pinFact   = 子淇【显式】说"记住X/钉住X" → 这是用户指令不是模型推断，不该被半自动护栏拦在
 *               staging 里（裂缝二：此前"说记住"最多进 staging 就卡死，对话里根本无法新增锚点）。
 *   两条边界清晰、互不重叠（团队原则11：宁可两个清晰函数，不要一个参数随上下文变义）。
 *
 * source 恒为 'user_said'（用户亲口要记的）；confidence/importance 给高值（显式指令=强信号）。
 * 幂等：同一 fact 原文已有未失效的 pinned 行 → 返回现有 id，不重复插（防重试/口误双发）。
 * 注：本函数不做矛盾消解（新锚点与旧锚点冲突时的 supersede 留给"删/留策略"那轮，属已知 backlog）。
 *
 * @param {{entity?:string|null, fact:string}} f
 * @returns {{id:number, deduped:boolean}}
 */
export function pinFact({ entity = null, fact } = {}) {
  const text = (fact ?? '').toString().trim();
  if (!text) throw new Error('[memory] pinFact: fact is required and non-empty');
  // facts.entity 是 NOT NULL（db schema）；但 pin 常无天然 entity（如"记住我对花粉过敏"），
  // 兜底成空串而非 null——空串满足 NOT NULL，且 prompt 渲染 `f.entity ? ...` 对空串友好（不加前缀）。
  const ent = (entity ?? '').toString().trim();
  // pinned fact 每轮注入 system 头部、永久驻留，过长=永久 token 税；超 200 字拒绝，让模型精简成一句话。
  if (text.length > 200) {
    throw new Error('[memory] pinFact: fact 过长（>200字），锚点应是一句话，请精简后再钉');
  }

  return tx((conn) => {
    // 幂等：同 fact 原文已 pinned 且未 superseded → 不重复插，返回现有 id。
    const ex = conn
      .prepare(`SELECT id FROM facts WHERE fact = ? AND pinned = 1 AND superseded_by IS NULL`)
      .get(text);
    if (ex) return { id: Number(ex.id), deduped: true };

    const now = nowMs();
    const info = conn
      .prepare(
        `INSERT INTO facts
           (entity, fact, source, confidence, created_at, valid_from,
            superseded_by, importance, last_accessed, access_count, pinned)
         VALUES (?, ?, 'user_said', 0.95, ?, ?, NULL, 0.9, NULL, 0, 1)`,
      )
      .run(ent, text, now, now);
    return { id: Number(info.lastInsertRowid), deduped: false };
  });
}

/**
 * unpinFact —— 取消一条锚点（pinned=1 → 0），让用户能纠正/撤回记错或过时的锚点。
 * 配合 pinFact 闭合本体"会忘、但忘得诚实、且能纠正"：用户说"别记X了/那条记错了"→ 模型调本函数。
 * 按 fact 原文【精确】匹配（不模糊匹配，避免误撤）；撤不到时由上层 unpin_fact 工具把当前锚点列出来让用户对准原话再试（原则11#3：状态可见，不靠猜）。
 * 不删行（fact 留在 facts 表作历史），只摘掉 pinned 身份 → pinnedFacts 不再注入它。
 * @param {string} fact  要取消的锚点原文（需与当前某条 pinned 完全一致）
 * @returns {{unpinned:boolean, changes:number}}
 */
export function unpinFact(fact) {
  const text = (fact ?? '').toString().trim();
  if (!text) throw new Error('[memory] unpinFact: fact is required and non-empty');
  return tx((conn) => {
    const info = conn
      .prepare(`UPDATE facts SET pinned = 0 WHERE fact = ? AND pinned = 1 AND superseded_by IS NULL`)
      .run(text);
    return { unpinned: info.changes > 0, changes: info.changes };
  });
}

// =====================================================================
// 4. 连续性引擎读接口：pinnedFacts / getSummary / retrieveMedia（context.mjs 组装五层时调）
// =====================================================================

/**
 * 锚点台账：pinned=1 的有效事实全量（确定性注入 system，防 tell #3 自相矛盾）。
 * 永不进有损摘要——硬状态与对话流物理剥离。按 importance 排，封顶 k 防失控。
 * @param {number} [k=12]
 * @returns {Array<{entity,fact,confidence,importance}>}
 */
export function pinnedFacts(k = 12) {
  const limit = Math.max(1, k | 0);
  const conn = getDb();
  return conn
    .prepare(
      `SELECT entity, fact, confidence, importance
       FROM facts
       WHERE pinned = 1 AND superseded_by IS NULL
       ORDER BY importance DESC, confidence DESC, id DESC
       LIMIT ?`,
    )
    .all(limit);
}

/**
 * 取某会话最新有效运行摘要（superseded=0 AND needs_review=0）。
 * 无摘要行（P0/P1 无 compaction，或新会话）→ 返回空默认：summary='' + covers_until_id=0
 * → 逐字近窗覆盖全程（id>0），正是首版无压缩的期望行为（P3 接 compaction 后才有真摘要）。
 * @param {string} sessionId
 * @returns {{summary:string, covers_until_id:number}}
 */
export function getSummary(sessionId) {
  const conn = getDb();
  const row = conn
    .prepare(
      `SELECT summary, covers_until_id
       FROM session_summaries
       WHERE session_id = ? AND superseded = 0 AND needs_review = 0
       ORDER BY id DESC LIMIT 1`,
    )
    .get(sessionId);
  if (!row) return { summary: '', covers_until_id: 0 };
  return { summary: row.summary || '', covers_until_id: row.covers_until_id || 0 };
}

/**
 * 媒体专项召回（防 tell #5 媒体指代被文本召回淹没）。
 * 媒体描述以 entity='media' 的 episode 落库；这里独立配额 LIKE 子串召回，不与文本抢 top-k
 * （FTS unicode61 对 [media#N]/中文子串不友好，故走 LIKE）。空 query → 最近 k 条媒体兜底。
 * @param {string} query
 * @param {number} [k=2]
 * @param {{sessionId?:string|null}} [opts]
 * @returns {Array<{id,ts,role,content,entity,task_id,score}>}
 */
export function retrieveMedia(query, k = 2, opts = {}) {
  const limit = Math.max(1, k | 0);
  const q = (query ?? '').trim();
  const sessionId = opts.sessionId ?? null;
  const conn = getDb();
  const now = nowMs();
  const sessFilter = sessionId ? ' AND session_id = ?' : '';

  let rows;
  if (q === '') {
    const params = sessionId ? [sessionId, limit] : [limit];
    rows = conn
      .prepare(
        `SELECT id, ts, role, content, entity, task_id
         FROM episodes
         WHERE entity = 'media'${sessFilter}
         ORDER BY ts DESC LIMIT ?`,
      )
      .all(...params);
  } else {
    const likeVal = '%' + escapeLike(q) + '%';
    const params = sessionId ? [likeVal, sessionId, limit] : [likeVal, limit];
    rows = conn
      .prepare(
        `SELECT id, ts, role, content, entity, task_id
         FROM episodes
         WHERE entity = 'media' AND content LIKE ? ESCAPE '\\'${sessFilter}
         ORDER BY ts DESC LIMIT ?`,
      )
      .all(...params);
  }
  return rows.map((r) => ({ ...r, score: recencyWeight(r.ts, now) }));
}

// =====================================================================
// 5. notes 黑匣子笔记层（# 快记的不可逆权威存储）
//    与 media_log 同位同构：黑匣子表存正本 + esm.mjs 另写一份 episodes 副本作召回入口。
//    只追加、永不改写、不进 episodes_fts。
//    ⚠️ 未来若实现对话压缩(compaction)：notes 天然不在 episodes 故不受压缩影响；但必须同时把召回
//       扩到能读 notes（union 或 #快记 episode 豁免压缩）。现在不做 = 不给空气编程（见 db.mjs session_summaries）。
// =====================================================================

/**
 * appendNote —— 把一条 # 快记原文写进不可逆黑匣子（notes 表）。
 * 与 appendEpisode 同风格（tx/nowMs/getDb，不收 db 参数）。这是"用户主动记的、绝不能丢"的权威正本，
 * 物理独立于会遗忘的 episodes 层（团队原则2：不可逆靠结构，不靠"episodes 碰巧 append-only"的纪律）。
 * @param {{ts?:number, sessionId?:string|null, content:string, source?:string}} n
 * @returns {number} 新 notes 行 id
 */
export function appendNote({ ts = nowMs(), sessionId = null, content, source = 'hashtag' } = {}) {
  const text = (content ?? '').toString();
  if (!text.trim()) throw new Error('[memory] appendNote: content is required and non-empty');
  return tx((conn) => {
    const info = conn
      .prepare(`INSERT INTO notes (ts, session_id, content, source) VALUES (?, ?, ?, ?)`)
      .run(ts, sessionId, text, String(source));
    return Number(info.lastInsertRowid);
  });
}

/**
 * listNotes —— 读出最近 k 条黑匣子笔记（按 ts 倒序）。
 * 现在仅供 selftest round-trip + 未来读出口；【暂不】接入 context 召回、【暂不】开 agent 工具
 * （那属于"对话压缩时代"的整合，现在做=给空气编程）。让黑匣子至少不是只写黑洞。
 * @param {number} [k=20]
 * @returns {Array<{id,ts,session_id,content,source}>}
 */
export function listNotes(k = 20) {
  const limit = Math.max(1, k | 0);
  return getDb()
    .prepare(`SELECT id, ts, session_id, content, source FROM notes ORDER BY ts DESC LIMIT ?`)
    .all(limit);
}

// =====================================================================
// 兼容别名：BLUEPRINT 早期命名 writeEpisode / writeFactToStaging。
// 权威契约用 appendEpisode / stageFact / retrieve / topFacts / promoteFact，
// 这里补别名让两套命名的调用方都能 work（不增加新语义，只是转发）。
// =====================================================================
export const writeEpisode = appendEpisode;
export const writeFactToStaging = stageFact;

// =====================================================================
// --selftest：离线、不联网、不依赖 LLM，自建临时 db 跑 memory 全链路断言。
// 必测：appendEpisode 返回 id、retrieve 在 fts5/like 两模式都返 top-k、
// entity boost 生效、空 query 返近况、stageFact 进 staging、promoteFact 幂等进 facts、
// topFacts 只取有效事实、FTS5 不可用时 retrieve 降级 LIKE 不崩。
// 运行：node memory.mjs --selftest
// =====================================================================
async function runSelftest() {
  const { mkdtempSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');

  let pass = 0;
  let fail = 0;
  function ok(cond, msg) {
    if (cond) {
      pass++;
      console.log('  ✓ ' + msg);
    } else {
      fail++;
      console.log('  ✗ ' + msg);
    }
  }

  // 用临时文件 db（WAL 不支持 :memory:），跑完删。
  const dir = mkdtempSync(join(tmpdir(), 'xw2-mem-'));
  const dbPath = join(dir, 'v2.db');
  process.env.XW2_DB_PATH = dbPath;

  // 这里直接调 db.mjs 的 initDb 建好真实 schema（含 FTS5 try/catch 与 MEMORY_MODE）。
  // 这样 selftest 跑的就是真实持久层，而不是 memory.mjs 自造的假表。
  let conn;
  try {
    conn = db.initDb(dbPath);
  } catch (e) {
    console.error('[memory selftest] initDb failed:', e.message);
    rmSync(dir, { recursive: true, force: true });
    process.exit(1);
  }

  const mode = memoryMode();
  console.log(`[memory selftest] MEMORY_MODE = ${mode}`);

  try {
    // ---- appendEpisode ----
    const baseTs = nowMs();
    const idA = appendEpisode({
      ts: baseTs - 10 * MS_PER_DAY, // 10 天前
      role: 'user',
      content: '子淇在一家公司做数据看板项目',
      entity: 'work',
    });
    const idB = appendEpisode({
      ts: baseTs - 1 * MS_PER_DAY, // 1 天前（更近）
      role: 'assistant',
      content: '记得每天学计算生物学，这是 Phase 1 的锚点',
      entity: 'study',
    });
    const idC = appendEpisode({
      ts: baseTs - 2 * MS_PER_DAY,
      role: 'user',
      content: '智能手环的数据看板要加一个趋势图',
      entity: 'work',
    });
    ok(idA > 0 && idB > idA && idC > idB, 'appendEpisode 返回递增正整数 id');

    // ---- retrieve：关键词召回（fts5 或 like 都该命中"手环"）----
    const r1 = retrieve('手环', 5);
    ok(Array.isArray(r1) && r1.length >= 1, `retrieve('手环') 命中至少 1 条 (got ${r1.length})`);
    ok(
      r1.every((x) => typeof x.score === 'number' && x.score >= 0),
      'retrieve 每条带非负 score',
    );
    ok(
      r1.some((x) => x.content.includes('手环')),
      'retrieve 命中内容含 "手环"',
    );

    // ---- retrieve top-k 上限 ----
    const rTop = retrieve('手环', 1);
    ok(rTop.length <= 1, 'retrieve top-k 截断生效 (k=1 → ≤1 条)');

    // ---- entity boost：query 命中两条 work 内容，指定 entity=work 应让 work 条目排前 ----
    const rEntity = retrieve('数据看板', 5, { entity: 'work' });
    ok(
      rEntity.length >= 1 && rEntity[0].entity === 'work',
      'entity boost 让目标 entity 排第一',
    );

    // ---- 空 query → 返回最近 k 条（近况），且按 ts 降序（idB 最新）----
    const rRecent = retrieve('', 3);
    ok(rRecent.length >= 1, '空 query 返回近况');
    ok(rRecent[0].id === idB, '空 query 近况按 ts 降序（最新 idB 在首）');

    // ---- 无命中词 → 返回空数组（LIKE）或 FTS 无 match（不崩）----
    const rMiss = retrieve('完全不存在的词xyzzy', 5);
    ok(Array.isArray(rMiss), 'retrieve 无命中也返回数组（不崩）');

    // ---- 特殊字符 query 不破坏 FTS MATCH / LIKE（防注入/语法炸）----
    const rWeird = retrieve('"; DROP TABLE episodes; --', 5);
    ok(Array.isArray(rWeird), 'retrieve 含特殊字符 query 安全返回（无注入/语法崩）');
    const stillThere = conn.prepare(`SELECT COUNT(*) AS c FROM episodes`).get();
    ok(stillThere.c === 3, 'episodes 表未被注入破坏（仍 3 行）');

    // ---- stageFact → facts_staging ----
    const sid = stageFact({ entity: 'work', fact: '喜欢爬山', source: 'user_said' });
    ok(sid > 0, 'stageFact 返回 staging id');
    const stRow = conn
      .prepare(`SELECT status, fact FROM facts_staging WHERE id = ?`)
      .get(sid);
    ok(stRow && stRow.status === 'pending', 'stageFact 落 facts_staging status=pending');

    // 非法 source 应抛
    let threw = false;
    try {
      stageFact({ fact: 'x', source: 'bogus' });
    } catch {
      threw = true;
    }
    ok(threw, 'stageFact 非法 source 抛错（提前校验 CHECK）');

    // 空 fact 应抛
    threw = false;
    try {
      stageFact({ fact: '   ' });
    } catch {
      threw = true;
    }
    ok(threw, 'stageFact 空 fact 抛错');

    // ---- promoteFact：staging → facts，幂等 ----
    const fid = promoteFact(sid, { confidence: 0.9, importance: 0.8 });
    ok(fid > 0, 'promoteFact 返回新 facts id');
    const fRow = conn
      .prepare(`SELECT fact, confidence, importance, superseded_by FROM facts WHERE id = ?`)
      .get(fid);
    ok(
      fRow && fRow.fact === '喜欢爬山' && fRow.confidence === 0.9 && fRow.superseded_by === null,
      'promoteFact 写入 facts（confidence/importance 正确，未 superseded）',
    );
    const stAfter = conn
      .prepare(`SELECT status FROM facts_staging WHERE id = ?`)
      .get(sid);
    ok(stAfter.status === 'accepted', 'promoteFact 同事务标记 staging=accepted');

    // 再次 promote 同一条 → 幂等返回 null，不重复入 facts
    const fid2 = promoteFact(sid);
    ok(fid2 === null, 'promoteFact 二次调用幂等返回 null（不重复 promote）');
    const factCount = conn.prepare(`SELECT COUNT(*) AS c FROM facts`).get();
    ok(factCount.c === 1, 'facts 表未因重复 promote 多出行');

    // promote 不存在的 id → null
    ok(promoteFact(999999) === null, 'promoteFact 不存在 id 返回 null');

    // ---- topFacts：只取有效（superseded_by IS NULL）----
    const tf = topFacts(null, 10);
    ok(tf.length === 1 && tf[0].fact === '喜欢爬山', 'topFacts 取有效事实');
    // 插入一条被 superseded 的事实，确认被过滤
    tx((c) => {
      c.prepare(
        `INSERT INTO facts (entity, fact, source, confidence, created_at, valid_from,
                            superseded_by, importance, last_accessed, access_count)
         VALUES ('work', '旧爱好', 'user_said', 0.5, ?, ?, 1, 0.9, NULL, 0)`,
      ).run(nowMs(), nowMs());
    });
    const tf2 = topFacts(null, 10);
    ok(
      tf2.every((x) => x.fact !== '旧爱好'),
      'topFacts 过滤掉已 superseded 的事实',
    );
    // entity 过滤
    const tfStudy = topFacts('study', 10);
    ok(tfStudy.length === 0, 'topFacts entity 过滤生效（study 无事实）');

    // ---- pinFact：用户显式钉锚点（pinned=1，跳过 staging），幂等。放在 topFacts 断言之后，
    //      避免多种一条 fact 污染上面"只有喜欢爬山一条"的断言。----
    const pf = pinFact({ entity: '居住', fact: '现居上海市区' });
    ok(pf.id > 0 && pf.deduped === false, 'pinFact 返回新 id、首次非 deduped');
    const pinnedRow = conn
      .prepare(`SELECT pinned, source FROM facts WHERE id = ?`)
      .get(pf.id);
    ok(
      pinnedRow && pinnedRow.pinned === 1 && pinnedRow.source === 'user_said',
      'pinFact 写入 pinned=1 + source=user_said',
    );
    ok(
      pinnedFacts(12).some((a) => a.fact === '现居上海市区'),
      'pinFact 的事实立即进 pinnedFacts 锚点台账（对话里能新增锚点 = 裂缝二已通）',
    );
    const pf2 = pinFact({ entity: '居住', fact: '现居上海市区' });
    ok(pf2.id === pf.id && pf2.deduped === true, 'pinFact 同 fact 幂等去重（返回现有 id，不重复插）');
    let pinThrew = false;
    try { pinFact({ fact: '   ' }); } catch { pinThrew = true; }
    ok(pinThrew, 'pinFact 空 fact 抛错');

    // 阻断 bug 回归（审查抓到）：无 entity 也能 pin（facts.entity NOT NULL → 兜底空串）
    const pfNoEnt = pinFact({ fact: '对花粉过敏' });
    ok(pfNoEnt.id > 0, 'pinFact 无 entity 也能成功（堵 entity NOT NULL 阻断 bug）');
    ok(
      conn.prepare(`SELECT entity FROM facts WHERE id = ?`).get(pfNoEnt.id)?.entity === '',
      'pinFact 无 entity → 存为空串（满足 NOT NULL）',
    );
    let pfLongThrew = false;
    try { pinFact({ fact: 'x'.repeat(201) }); } catch { pfLongThrew = true; }
    ok(pfLongThrew, 'pinFact 超 200 字拒绝（防永久 token 税）');

    // 同源 bug：promoteFact 处理空 entity 的 staging 不崩（staging.entity 可空 vs facts NOT NULL）
    const sidNoEnt = stageFact({ fact: '一条没有 entity 的推断事实' });
    ok(promoteFact(sidNoEnt) > 0, 'promoteFact 空 entity staging 不崩（同源兜底）');

    // unpinFact：精确匹配取消锚点（pin↔unpin 闭合"能纠正"）
    ok(unpinFact('对花粉过敏').unpinned === true, 'unpinFact 精确匹配 → 取消成功');
    ok(
      !pinnedFacts(20).some((a) => a.fact === '对花粉过敏'),
      'unpin 后该事实不再出现在锚点台账',
    );
    ok(unpinFact('压根没钉过的东西').unpinned === false, 'unpinFact 匹配不到 → unpinned=false（上层会列出当前锚点让对准）');

    // ---- notes 黑匣子层 round-trip ----
    const nid = appendNote({ sessionId: 'wecom:test', content: '中午和老王吃饭聊了项目', source: 'hashtag' });
    ok(nid > 0, 'appendNote 返回 notes 行 id');
    ok(
      listNotes(10).some((n) => n.content === '中午和老王吃饭聊了项目' && n.session_id === 'wecom:test'),
      'listNotes 读回刚写的笔记（黑匣子可 round-trip）',
    );
    let noteThrew = false;
    try { appendNote({ content: '   ' }); } catch { noteThrew = true; }
    ok(noteThrew, 'appendNote 空 content 抛错');

    // ---- 兼容别名 ----
    ok(writeEpisode === appendEpisode, 'writeEpisode 别名指向 appendEpisode');
    ok(writeFactToStaging === stageFact, 'writeFactToStaging 别名指向 stageFact');

    // ---- recencyWeight 纯函数性质：更近 → 权重更大，未来时间夹到权重 1 ----
    const wNear = recencyWeight(baseTs - 1 * MS_PER_DAY, baseTs);
    const wFar = recencyWeight(baseTs - 30 * MS_PER_DAY, baseTs);
    ok(wNear > wFar, 'recencyWeight 近 > 远');
    ok(Math.abs(recencyWeight(baseTs + MS_PER_DAY, baseTs) - 1) < 1e-9, 'recencyWeight 未来时间夹到 1');
  } finally {
    try {
      conn && conn.close && conn.close();
    } catch {
      /* 关闭失败不影响 selftest 结论 */
    }
    rmSync(dir, { recursive: true, force: true });
  }

  console.log(`\n[memory selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

// CLI 入口：仅当本文件被直接 node 执行且带 --selftest 时跑自检（参照团队既有 selftest 写法）。
if (
  process.argv.includes('--selftest') &&
  import.meta.url === (await import('node:url')).pathToFileURL(process.argv[1] || '').href
) {
  await runSelftest();
}
