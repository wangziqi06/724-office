// =====================================================================
// db.mjs —— 小王 v2 持久层内核（A 模块）
//
// 职责单一：拥有「唯一的单写 SQLite 连接」+ 唯一权威 schema + 事务包装器。
// 所有其它模块只通过 getDb()/initDb() 拿连接，绝不自己 new DatabaseSync。
// 为什么单写：node:sqlite 是同步 API，全项目串行写。WAL 在这里的真实价值
// 只是 crash 安全 + 读不脏读，不是并发——开第二写连接只会换来 SQLITE_BUSY。
//
// 时间戳口径：所有 *_at / ts / fire_at = epoch ms（INTEGER），统一经 nowMs() 取。
// JSON 字段统一以 JSON.stringify 的 TEXT 存（node:sqlite 对 TEXT 友好、可读、好 debug）。
// =====================================================================

import { DatabaseSync } from 'node:sqlite';
import { dirname, isAbsolute, join } from 'node:path';
import { mkdirSync } from 'node:fs';

// ---------------------------------------------------------------------
// 常量集中（禁止魔法数散落，所有阈值在此导出复用）
// 单位：ms，除非显式标注。这些是 worker / durable / llm 各模块共用的护栏阈值。
// ---------------------------------------------------------------------
export const TICK_MS = 5000;              // worker setInterval 周期
export const STALE_TASK_MS = 60000;       // task heartbeat 静默超此 → 判崩溃重放（> tick 多倍）
export const MAX_TASK_ATTEMPTS = 5;       // 任务重跑上限，超过 → status='dead'（毒任务）
export const MAX_OUTBOX_ATTEMPTS = 5;     // outbox 单条发送重试上限，超过 → status='failed'
export const RECENCY_TAU_DAYS = 14;       // 记忆召回 recency 衰减时间常数（天）
export const LLM_TIMEOUT_MS = 30000;      // LLM fetch 超时（致命纪律②：外部调用必带 timeout）
export const HTTP_TIMEOUT_MS = 15000;     // http_get 工具 / 企微 API 超时

// 连续性引擎常量（P0+：在线上下文组装 / 召回）。阈值标 emerging，待真实数据迭代（见 BLUEPRINT_CONTINUITY §6）。
export const RECENT_TURNS_LIMIT = 20;     // 逐字近窗：最近 N 条 user/assistant 原文（≈10 轮）。越大连续感越强但每轮 token 越高。
export const HEDGE_THRESHOLD = 0.15;      // 召回最高分低于此 → recallWeak=true，让小王诚实 hedge（"印象里提过，细节记不准"）。起步值，待调。
export const RELEVANCE_FLOOR = 0.02;      // 相关性闸：score 低于此的召回条目丢弃（滤掉关键词巧合的陈旧条目）。起步值，待调。

// agentic 内护栏常量（loop.mjs 复用，集中在此避免散落）
export const GUARDS = Object.freeze({
  maxTurns: 20,
  wallclockMs: 180000,
  noProgressRepeats: 2,
});

// ---------------------------------------------------------------------
// 时间源：全项目唯一时钟。禁止散落 Date.now()/new Date()，便于 selftest 注入固定时钟。
// 为什么用可替换的内部变量：selftest 要能把"现在"钉死才能断言 timer 触发/陈旧判定。
// ---------------------------------------------------------------------
let _clock = () => Date.now();
export function nowMs() {
  return _clock();
}
// 仅 selftest 用：注入固定/可控时钟。生产代码不应调用。
export function __setClockForTest(fn) {
  _clock = typeof fn === 'function' ? fn : (() => Date.now());
}

// ---------------------------------------------------------------------
// MEMORY_MODE：由 initDb 在 FTS5 建表成功/失败后赋值。
// memory.mjs 读它决定召回路径（'fts5' 走 BM25×recency，'like' 走 LIKE 降级）。
// 用 getter 导出，保证 builder 拿到的是最终值而非初始 undefined。
// ---------------------------------------------------------------------
let _memoryMode = 'like'; // 安全默认：未初始化前按降级路径，绝不假装有 FTS5
export function getMemoryMode() {
  return _memoryMode;
}

// ---------------------------------------------------------------------
// 单写连接单例。重复 initDb()/getDb() 返同一连接（单写连接铁律）。
// ---------------------------------------------------------------------
let _db = null;

/**
 * 解析 db 路径。默认 ./v2.db（相对模块目录），selftest 经 XW2_DB_PATH 指临时文件。
 * 注意：WAL 不支持 ':memory:'，所以 selftest 必须用真实临时文件。
 */
function resolveDbPath(dbPath) {
  const p = dbPath || process.env.XW2_DB_PATH || join(import.meta.dirname, 'v2.db');
  return isAbsolute(p) ? p : join(import.meta.dirname, p);
}

/**
 * 打开/创建唯一单写连接，跑 PRAGMA + 全部 CREATE TABLE + 索引 + 可选 FTS5 + heartbeat 初始化。
 * 幂等（IF NOT EXISTS），重复调用返同一连接。
 */
export function initDb(dbPath = process.env.XW2_DB_PATH) {
  if (_db) return _db;

  const path = resolveDbPath(dbPath);
  // 确保父目录存在（selftest 指向 scratchpad/tmp 子目录时可能不存在）
  try {
    mkdirSync(dirname(path), { recursive: true });
  } catch (e) {
    // 目录已存在等错误无害；真正打不开会在 new DatabaseSync 抛，留给下面
  }

  const db = new DatabaseSync(path);

  // ---- PRAGMA：连接打开后立即执行，顺序固定（见 conventions）----
  // 为什么 synchronous=FULL：n=1 负载低，crash 安全比吞吐重要，宁可慢不可丢已确认的写。
  db.exec('PRAGMA journal_mode=WAL;');
  db.exec('PRAGMA synchronous=FULL;');
  db.exec('PRAGMA busy_timeout=5000;');
  db.exec('PRAGMA foreign_keys=ON;');

  // ---- 1. tasks ----
  db.exec(`
    CREATE TABLE IF NOT EXISTS tasks (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      kind            TEXT    NOT NULL,
      status          TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','running','done','failed','dead')),
      next_run_at     INTEGER,
      idempotency_key TEXT    UNIQUE,
      payload         TEXT    NOT NULL DEFAULT '{}',
      attempts        INTEGER NOT NULL DEFAULT 0,
      result          TEXT,
      last_error      TEXT,
      created_at      INTEGER NOT NULL,
      updated_at      INTEGER NOT NULL,
      heartbeat_at    INTEGER
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_tasks_status_next ON tasks (status, next_run_at);`);

  // ---- 2. steps（确定性任务 step 级 memo）----
  db.exec(`
    CREATE TABLE IF NOT EXISTS steps (
      task_id      INTEGER NOT NULL,
      step_seq     INTEGER NOT NULL,
      status       TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','complete')),
      tool_name    TEXT,
      params       TEXT,
      return_value TEXT,
      attempts     INTEGER NOT NULL DEFAULT 0,
      ts           INTEGER NOT NULL,
      PRIMARY KEY (task_id, step_seq)
    );
  `);

  // ---- 3. timers ----
  db.exec(`
    CREATE TABLE IF NOT EXISTS timers (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      fire_at        INTEGER NOT NULL,
      task_id        INTEGER,
      payload        TEXT    NOT NULL DEFAULT '{}',
      catchup_policy TEXT    NOT NULL DEFAULT 'once'
                             CHECK (catchup_policy IN ('skip','once')),
      status         TEXT    NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending','fired','cancelled')),
      created_at     INTEGER NOT NULL
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_timers_status_fire ON timers (status, fire_at);`);

  // ---- 4. outbox（一切对外副作用的唯一出口，dedup_hash UNIQUE 做尽力去重）----
  db.exec(`
    CREATE TABLE IF NOT EXISTS outbox (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      channel    TEXT    NOT NULL,
      target     TEXT    NOT NULL,
      content    TEXT    NOT NULL,
      dedup_hash TEXT    NOT NULL UNIQUE,
      status     TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','sent','failed')),
      attempts   INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      created_at INTEGER NOT NULL,
      sent_at    INTEGER
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox (status, created_at);`);

  // ---- 5. episodes（只追加对话/事件流）----
  db.exec(`
    CREATE TABLE IF NOT EXISTS episodes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         INTEGER NOT NULL,
      session_id TEXT,
      role       TEXT    NOT NULL,
      content    TEXT    NOT NULL,
      entity     TEXT,
      task_id    INTEGER
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes (ts);`);
  // 连续性引擎：逐字近窗 / 压缩边界 / sessionId 过滤都按 (session_id, id) 查，单一排序轴=id（致命纪律⑤）。
  db.exec(`CREATE INDEX IF NOT EXISTS idx_episodes_session_id ON episodes (session_id, id);`);

  // ---- 6. facts（半自动，首版不自动抽取）----
  db.exec(`
    CREATE TABLE IF NOT EXISTS facts (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      entity        TEXT    NOT NULL,
      fact          TEXT    NOT NULL,
      source        TEXT    NOT NULL
                            CHECK (source IN ('user_said','inferred','external')),
      confidence    REAL    NOT NULL DEFAULT 0.5,
      created_at    INTEGER NOT NULL,
      valid_from    INTEGER,
      superseded_by INTEGER,
      importance    REAL    NOT NULL DEFAULT 0.5,
      last_accessed INTEGER,
      access_count  INTEGER NOT NULL DEFAULT 0,
      pinned        INTEGER NOT NULL DEFAULT 0
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts (entity);`);
  // pinned 列（锚点台账子集）：对已存在的旧 facts 表补列。ALTER 重复执行会因列已存在抛错 → try/catch 吞掉（幂等）。
  // 新建库走上面的 CREATE TABLE 已带 pinned，这条只为旧库补齐，两路都到位。
  try {
    db.exec(`ALTER TABLE facts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;`);
  } catch (e) {
    /* duplicate column name → 已有 pinned，幂等无害 */
  }
  db.exec(`CREATE INDEX IF NOT EXISTS idx_facts_pinned ON facts (pinned);`);

  // ---- 6b. facts_staging（memory_write 工具落点，等人工确认）----
  db.exec(`
    CREATE TABLE IF NOT EXISTS facts_staging (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      entity     TEXT,
      fact       TEXT    NOT NULL,
      source     TEXT    NOT NULL DEFAULT 'inferred'
                         CHECK (source IN ('user_said','inferred','external')),
      status     TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','accepted','rejected')),
      created_at INTEGER NOT NULL
    );
  `);

  // ---- 6c. session_summaries（运行摘要，append-only 版本化；P0/P1 建表但暂不写，P3 compaction.mjs 才填）----
  // 最新有效行 = superseded=0 AND needs_review=0。崩溃重放生成不同摘要时两版都在库可对账。
  // P0+P1 阶段无写入 → getSummary 永远返回空默认 → 逐字近窗覆盖全程（无压缩），正是首版期望行为。
  db.exec(`
    CREATE TABLE IF NOT EXISTS session_summaries (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id      TEXT    NOT NULL,
      summary         TEXT    NOT NULL DEFAULT '',
      covers_until_id INTEGER NOT NULL DEFAULT 0,
      superseded      INTEGER NOT NULL DEFAULT 0,
      needs_review    INTEGER NOT NULL DEFAULT 0,
      updated_at      INTEGER NOT NULL
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries (session_id, superseded);`);

  // ---- 6d. media_log（媒体两层：不可逆原始字节落盘 file_path + 可再生 transcript；原则9 绝不自动删原始字节）----
  // id 即 [media#N] 锚点；描述/转写另写普通 episode(entity='media', task_id=media_log.id) 走召回。
  db.exec(`
    CREATE TABLE IF NOT EXISTS media_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         INTEGER NOT NULL,
      session_id TEXT,
      sender_id  TEXT,
      kind       TEXT    NOT NULL CHECK (kind IN ('image','voice','file')),
      file_path  TEXT    NOT NULL,
      transcript TEXT,
      model      TEXT,
      coded_at   INTEGER
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_media_session ON media_log (session_id, ts);`);

  // ---- 6e. notes（# 快记黑匣子：用户主动记的笔记正本，不可逆、append-only）----
  // 与 media_log 同位：黑匣子表存正本，esm.mjs 另写一份 episodes 副本(entity='event')走召回。
  // 放 initDb 而非 esm.initEsmSchema：getDb() 自动建 initDb 的表，保证"打开库即存在"；
  // notes 不进 episodes_fts、不进 assertNoGamification（它不是 ESM 查看端表）。
  db.exec(`
    CREATE TABLE IF NOT EXISTS notes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         INTEGER NOT NULL,
      session_id TEXT,
      content    TEXT    NOT NULL,
      source     TEXT    NOT NULL DEFAULT 'hashtag'
    );
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes (ts);`);

  // ---- 7. heartbeat（单行 id=1，worker 每 tick UPSERT，watchdog 读这行判存活）----
  db.exec(`
    CREATE TABLE IF NOT EXISTS heartbeat (
      id       INTEGER PRIMARY KEY CHECK (id = 1),
      ts       INTEGER NOT NULL,
      progress TEXT
    );
  `);
  // 初始化心跳行（已存在则不动，幂等）
  db.prepare(
    `INSERT OR IGNORE INTO heartbeat (id, ts, progress) VALUES (1, ?, 'init')`
  ).run(nowMs());

  // ---- 8. episodes_fts（可选 FTS5）----
  // 为什么 try/catch：红队指出 FTS5 在某些 node:sqlite 编译下不存在，建表即崩。
  // 降级是代码不是流程——建失败就落 LIKE 模式，运行时一个 MEMORY_MODE 标志位决定召回 SQL。
  // 两段（虚表 + 触发器）必须同一 try 块：要么都成，要么都不建（避免触发器引用不存在的虚表）。
  try {
    db.exec(`
      CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
        content,
        entity,
        content='episodes',
        content_rowid='id',
        tokenize='unicode61'
      );
    `);
    db.exec(`
      CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, content, entity) VALUES (new.id, new.content, new.entity);
      END;
    `);
    db.exec(`
      CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, content, entity) VALUES('delete', old.id, old.content, old.entity);
      END;
    `);
    db.exec(`
      CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, content, entity) VALUES('delete', old.id, old.content, old.entity);
        INSERT INTO episodes_fts(rowid, content, entity) VALUES (new.id, new.content, new.entity);
      END;
    `);
    _memoryMode = 'fts5';
  } catch (e) {
    // 失败要响不静默吞：留证据说明为什么降级了，方便 ECS 上复测排障。
    console.warn('[db] FTS5 unavailable, memory retrieval degraded to LIKE:', e.message);
    _memoryMode = 'like';
  }

  _db = db;
  return _db;
}

/**
 * 返回 initDb 建好的单例连接；未初始化则内部调 initDb()。
 * 所有其它模块只通过这里拿连接，绝不自己 new DatabaseSync。
 */
export function getDb() {
  return _db || initDb();
}

// ---------------------------------------------------------------------
// 事务包装器 tx()
// BEGIN IMMEDIATE → fn(db) → COMMIT；fn 抛错则 ROLLBACK 并重新抛出。
//
// 致命纪律③：fn 内严禁任何 await/fetch/HTTP/LLM——事务持锁期间做外部调用会
// 把 DB 锁拖到网络延迟级别，单写连接下整个 worker 会被堵死。
// 本包装器是同步的，从签名上就杜绝了 await（传 async fn 进来 fn(db) 会立刻返回
// 一个未 await 的 Promise，COMMIT 会在副作用落库前发生——所以调用方必须传同步 fn）。
//
// 为什么 BEGIN IMMEDIATE 而非 DEFERRED：立刻拿写锁，避免"读着读着升级写锁"时
// 撞上 busy_timeout 才失败；单写连接下 IMMEDIATE 不会和自己冲突。
// ---------------------------------------------------------------------
export function tx(fn) {
  const db = getDb();
  db.exec('BEGIN IMMEDIATE;');
  try {
    const result = fn(db);
    // 防呆：若 fn 误返回 Promise，说明调用方传了 async（违反致命纪律③），立刻炸响而非静默错误提交。
    // 直接抛，让下面统一的 catch 去 ROLLBACK（避免重复 ROLLBACK 触发 "no transaction" 噪音日志）。
    if (result && typeof result.then === 'function') {
      throw new Error('[db] tx() fn must be synchronous — no await/fetch/LLM inside a transaction (致命纪律③)');
    }
    db.exec('COMMIT;');
    return result;
  } catch (e) {
    // ROLLBACK 自身也可能抛（如连接已坏），但优先抛原始业务错误，留真因证据
    try {
      db.exec('ROLLBACK;');
    } catch (rbErr) {
      console.error('[db] ROLLBACK failed after error: %s', rbErr.message);
    }
    throw e;
  }
}

// 仅 selftest / 显式重置用：关闭并清空单例（生产长驻进程不调用）。
export function __closeForTest() {
  if (_db) {
    try {
      _db.close();
    } catch (e) {
      /* 已关闭等无害 */
    }
    _db = null;
  }
  _memoryMode = 'like';
}

// =====================================================================
// --selftest：离线、不联网，验证 schema 建立 + FTS5 分支 + tx 回滚 + 单例。
// 运行：XW2_DB_PATH=<tmp> node db.mjs --selftest
// =====================================================================
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { rmSync } from 'node:fs';

function runSelftest() {
  let pass = 0;
  let fail = 0;
  const ok = (cond, msg) => {
    if (cond) {
      pass++;
      console.log('  ✓ ' + msg);
    } else {
      fail++;
      console.log('  ✗ ' + msg);
    }
  };

  // 用临时文件（WAL 不支持 :memory:）
  const tmpPath = join(tmpdir(), `xw2-db-selftest-${process.pid}-${Date.now()}.db`);
  process.env.XW2_DB_PATH = tmpPath;

  try {
    const db = initDb(tmpPath);
    ok(!!db, 'initDb 返回连接');
    ok(getDb() === db, 'getDb 返回同一单例');
    ok(initDb() === db, 'initDb 重复调用返同一单例（幂等）');

    // MEMORY_MODE 应已确定（本机 FTS5 可用 → fts5；不可用 → like，两者都合法）
    const mode = getMemoryMode();
    ok(mode === 'fts5' || mode === 'like', `MEMORY_MODE 已确定为 '${mode}'`);

    // 全部表存在
    const tableNames = db
      .prepare(`SELECT name FROM sqlite_master WHERE type='table'`)
      .all()
      .map((r) => r.name);
    for (const t of [
      'tasks', 'steps', 'timers', 'outbox',
      'episodes', 'facts', 'facts_staging', 'heartbeat',
      'session_summaries', 'media_log', 'notes',
    ]) {
      ok(tableNames.includes(t), `表 ${t} 已建`);
    }
    // facts.pinned 列存在（锚点台账）
    const factCols = db.prepare(`PRAGMA table_info(facts)`).all().map((c) => c.name);
    ok(factCols.includes('pinned'), 'facts.pinned 列已建');

    // 索引存在
    const idxNames = db
      .prepare(`SELECT name FROM sqlite_master WHERE type='index'`)
      .all()
      .map((r) => r.name);
    for (const i of [
      'idx_tasks_status_next', 'idx_timers_status_fire',
      'idx_outbox_status', 'idx_episodes_ts', 'idx_facts_entity',
      'idx_episodes_session_id', 'idx_facts_pinned',
      'idx_summaries_session', 'idx_media_session', 'idx_notes_ts',
    ]) {
      ok(idxNames.includes(i), `索引 ${i} 已建`);
    }

    // heartbeat 初始化行存在且 id=1
    const hb = db.prepare('SELECT * FROM heartbeat').all();
    ok(hb.length === 1 && hb[0].id === 1, 'heartbeat 初始化单行 id=1');

    // PRAGMA 生效
    const jm = db.prepare('PRAGMA journal_mode').get();
    ok(jm.journal_mode === 'wal', 'journal_mode=WAL 生效');
    const fk = db.prepare('PRAGMA foreign_keys').get();
    ok(fk.foreign_keys === 1, 'foreign_keys=ON 生效');

    // FTS5 模式下：插一条 episode，触发器应自动同步虚表，能 MATCH 到
    if (mode === 'fts5') {
      db.prepare(
        'INSERT INTO episodes (ts, role, content, entity) VALUES (?, ?, ?, ?)'
      ).run(nowMs(), 'user', '磐石核测试 lobster core durable', 'selftest');
      const hit = db
        .prepare(`SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH ?`)
        .all('lobster');
      ok(hit.length === 1, 'FTS5 触发器自动同步虚表，MATCH 命中');
    } else {
      ok(true, 'FTS5 不可用，已降级 LIKE（跳过虚表断言）');
    }

    // tx() 提交：插一条 outbox
    tx((d) => {
      d.prepare(
        `INSERT INTO outbox (channel, target, content, dedup_hash, created_at) VALUES (?,?,?,?,?)`
      ).run('wecom', 'owner', 'hi', 'hash-commit-01', nowMs());
    });
    const committed = db.prepare(`SELECT * FROM outbox WHERE dedup_hash=?`).all('hash-commit-01');
    ok(committed.length === 1, 'tx() 正常提交落库');

    // tx() 回滚：fn 抛错，行不应落库
    let threw = false;
    try {
      tx((d) => {
        d.prepare(
          `INSERT INTO outbox (channel, target, content, dedup_hash, created_at) VALUES (?,?,?,?,?)`
        ).run('wecom', 'owner', 'rollme', 'hash-rollback-01', nowMs());
        throw new Error('boom');
      });
    } catch (e) {
      threw = e.message === 'boom';
    }
    ok(threw, 'tx() fn 抛错被重新抛出');
    const rolled = db.prepare(`SELECT * FROM outbox WHERE dedup_hash=?`).all('hash-rollback-01');
    ok(rolled.length === 0, 'tx() 抛错后 ROLLBACK，行未落库');

    // tx() 拒绝 async fn（致命纪律③ 防呆）
    let rejectedAsync = false;
    try {
      tx(async () => 1);
    } catch (e) {
      rejectedAsync = /synchronous/.test(e.message);
    }
    ok(rejectedAsync, 'tx() 拒绝 async fn（致命纪律③ 防呆生效）');

    // nowMs 时钟可注入
    __setClockForTest(() => 123456789);
    ok(nowMs() === 123456789, 'nowMs() 时钟可注入（selftest 用）');
    __setClockForTest(null);
    ok(nowMs() > 1e12, 'nowMs() 时钟可复位为真实时间');
  } catch (e) {
    fail++;
    console.log('  ✗ selftest 异常: ' + e.stack);
  } finally {
    __closeForTest();
    // 清理临时文件 + WAL/SHM 伴生文件
    for (const ext of ['', '-wal', '-shm']) {
      try {
        rmSync(tmpPath + ext, { force: true });
      } catch (e) {
        /* ignore */
      }
    }
  }

  console.log(`\n[db.mjs selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href && process.argv.includes('--selftest')) {
  runSelftest();
}
