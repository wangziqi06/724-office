// =====================================================================
// durable.mjs —— 小王 v2 durable 跨天执行内核（A 模块）
//
// 四表（tasks/steps/timers/outbox）之上的持久执行原语。核心哲学：
//   「可靠性来自结构，不来自聪明」——内存绝不存不可丢状态，进程崩了重启
//   就能从库里把活捡回来接着干。
//
// 分级 durable（红队收口后的语义，别假装 effectively-once）：
//   - 确定性任务（kind='deterministic'，定时器触发的固定动作）走 step 级 memo：
//     重放时 status='complete' 的 step 跳过、直接返存值（getStepMemo）。
//   - agentic LLM 任务（kind='agentic'）不做精确断点续：崩溃后整体重跑，
//     发送类副作用靠 outbox.dedup_hash UNIQUE 去重（at-least-once + 尽力去重）。
//
// 致命纪律：所有状态变更走 db.tx()（同步、原子、单条 UPDATE 同时改 status+result）；
//   tx 内严禁 await/fetch/LLM（LLM/HTTP 在事务外，由 loop/adapter 负责）。
// =====================================================================

import { createHash } from 'node:crypto';
import {
  getDb,
  tx,
  nowMs,
  MAX_TASK_ATTEMPTS,
  MAX_OUTBOX_ATTEMPTS,
  STALE_TASK_MS,
  TICK_MS,
} from './db.mjs';

// ---------------------------------------------------------------------
// dedup_hash 算法（见 conventions）：sha256 hex 前 16 字符，规范化后拼接。
// 副作用去重的唯一真相来源。parts 为有序数组，调用方按业务定义顺序。
// 为什么前 16 字符：碰撞概率对 n=1 一天几条消息完全够，且短、可读、好 debug。
// ---------------------------------------------------------------------
export function dedupHash(parts) {
  const norm = parts.map((p) => String(p ?? '').trim()).join(' ');
  return createHash('sha256').update(norm).digest('hex').slice(0, 16);
}

// JSON 读写小工具：库里统一存 JSON TEXT，空值存 'null'，读出一律 JSON.parse。
// 绝不存裸对象——AI-first 可读 + 避免 [object Object] 这种静默损坏。
function j(v) {
  return JSON.stringify(v ?? null);
}
function unj(s) {
  if (s == null) return null;
  try {
    return JSON.parse(s);
  } catch (e) {
    // 数据损坏要响不静默吞：返回原始字符串 + 留证据，便于排障而非假装解析成功
    console.error('[durable] JSON.parse failed, returning raw: %s', e.message);
    return s;
  }
}

// =====================================================================
// 1. createTask —— 派任务。业务写入与建 task 行同事务原子提交（致命纪律）。
//
// idempotencyKey 命中 UNIQUE 冲突 → 不重复建，返回已存在 taskId + deduped=true。
// businessFn(db) 在同一 tx 内执行业务写入（如"记一条 episode + 派任务"），
// 二者要么都成要么都不成。businessFn 同样禁 await/fetch（tx 是同步的）。
// =====================================================================
export function createTask(
  { kind, payload = {}, idempotencyKey = null, nextRunAt = null },
  businessFn = null
) {
  if (kind !== 'deterministic' && kind !== 'agentic') {
    throw new Error(`[durable] createTask: kind must be 'deterministic'|'agentic', got '${kind}'`);
  }
  const now = nowMs();
  const runAt = nextRunAt == null ? now : nextRunAt;

  return tx((db) => {
    // 幂等：先查 idempotencyKey 是否已存在（UNIQUE 列）。命中即返回旧 task，不重复建。
    // 为什么先查而非 INSERT OR IGNORE 后读：要拿到既存行的真实 id 返回给调用方。
    if (idempotencyKey != null) {
      const existing = db
        .prepare('SELECT id FROM tasks WHERE idempotency_key = ?')
        .get(idempotencyKey);
      if (existing) {
        return { taskId: existing.id, deduped: true };
      }
    }

    // 业务写入与建 task 行在同一事务（致命纪律：业务+任务原子提交）。
    if (businessFn) {
      const r = businessFn(db);
      if (r && typeof r.then === 'function') {
        // 防呆：businessFn 不能是 async（会破坏事务原子性）
        throw new Error('[durable] businessFn must be synchronous (no await/fetch inside tx)');
      }
    }

    const res = db
      .prepare(
        `INSERT INTO tasks
           (kind, status, next_run_at, idempotency_key, payload, attempts, created_at, updated_at)
         VALUES (?, 'pending', ?, ?, ?, 0, ?, ?)`
      )
      .run(kind, runAt, idempotencyKey, j(payload), now, now);

    return { taskId: Number(res.lastInsertRowid), deduped: false };
  });
}

// 别名：任务书要求导出 enqueueTask；与 createTask 同义（契约用 createTask，编排用 enqueueTask）。
export const enqueueTask = createTask;

// =====================================================================
// 2. scheduleTimer —— "3天后提醒"落 timers 行。
// fireAt = epoch ms。"3天后" = nowMs() + 3*86400000。
// catchupPolicy: 'once'（过期补跑一次，如一次性提醒）| 'skip'（只跑下一次，如每日提醒）。
// =====================================================================
export function scheduleTimer({ fireAt, taskId = null, payload = {}, catchupPolicy = 'once' }) {
  if (typeof fireAt !== 'number' || !Number.isFinite(fireAt)) {
    throw new Error('[durable] scheduleTimer: fireAt must be a finite epoch-ms number');
  }
  if (catchupPolicy !== 'once' && catchupPolicy !== 'skip') {
    throw new Error(`[durable] scheduleTimer: catchupPolicy must be 'once'|'skip', got '${catchupPolicy}'`);
  }
  const now = nowMs();
  return tx((db) => {
    const res = db
      .prepare(
        `INSERT INTO timers (fire_at, task_id, payload, catchup_policy, status, created_at)
         VALUES (?, ?, ?, ?, 'pending', ?)`
      )
      .run(fireAt, taskId, j(payload), catchupPolicy, now);
    return Number(res.lastInsertRowid);
  });
}

// =====================================================================
// 3. step 级 memo —— 确定性任务重放用。
//
// getStepMemo: 重放时查 step，complete 则返存值（跳过重做）；否则 undefined（需真执行）。
// recordStep: 执行完一个 step 后 UPSERT 为 complete + 存 return_value。
// markComplete: 任务书要求的别名，语义等同 recordStep（标记某 step 完成并存值）。
// =====================================================================
export function getStepMemo(taskId, stepSeq) {
  const db = getDb();
  const row = db
    .prepare('SELECT status, return_value FROM steps WHERE task_id = ? AND step_seq = ?')
    .get(taskId, stepSeq);
  if (row && row.status === 'complete') {
    return unj(row.return_value);
  }
  return undefined; // 表示这个 step 还没做过 / 没做完，调用方需真正执行
}

export function recordStep(taskId, stepSeq, { toolName = null, params = null, returnValue = null }) {
  const now = nowMs();
  tx((db) => {
    // UPSERT：首次 INSERT；重放或重试同 step 时 ON CONFLICT 更新为 complete + 累加 attempts。
    // 为什么 UPSERT 而非先查后写：一条语句原子完成，避免竞态（虽单写串行，但语义更干净）。
    db.prepare(
      `INSERT INTO steps (task_id, step_seq, status, tool_name, params, return_value, attempts, ts)
         VALUES (?, ?, 'complete', ?, ?, ?, 1, ?)
       ON CONFLICT(task_id, step_seq) DO UPDATE SET
         status       = 'complete',
         tool_name    = excluded.tool_name,
         params       = excluded.params,
         return_value = excluded.return_value,
         attempts     = steps.attempts + 1,
         ts           = excluded.ts`
    ).run(taskId, stepSeq, toolName, j(params), j(returnValue), now);
  });
}

// 别名：任务书要求导出 markComplete。等同 recordStep（标记 step 完成并落 memo）。
export const markComplete = recordStep;

// =====================================================================
// 4. recoverStaleTasks —— 崩溃恢复。
//
// 扫 status IN ('pending','running') 且 heartbeat 陈旧的 task：attempts+1，返回 taskId 供重放/重跑。
// attempts 超 MAX_TASK_ATTEMPTS 的置 status='dead'（毒任务）并不返回——避免崩溃-重跑死循环。
//
// 为什么 pending 也算：派了但 worker 还没起跑就崩，heartbeat_at 是 NULL，也得捡回来。
// =====================================================================
export function recoverStaleTasks(staleMs = STALE_TASK_MS) {
  const now = nowMs();
  const cutoff = now - staleMs;
  return tx((db) => {
    const stale = db
      .prepare(
        `SELECT id, attempts FROM tasks
          WHERE status IN ('pending','running')
            AND (heartbeat_at IS NULL OR heartbeat_at < ?)
            AND (next_run_at IS NULL OR next_run_at <= ?)`
      )
      .all(cutoff, now);

    const revived = [];
    for (const t of stale) {
      const nextAttempts = t.attempts + 1;
      if (nextAttempts > MAX_TASK_ATTEMPTS) {
        // 毒任务：超重试上限，标 dead 留证据，不再返回（不参与重放）
        db.prepare(
          `UPDATE tasks
              SET status='dead', attempts=?, last_error=?, updated_at=?
            WHERE id=?`
        ).run(
          nextAttempts,
          `exceeded MAX_TASK_ATTEMPTS=${MAX_TASK_ATTEMPTS} on recovery`,
          now,
          t.id
        );
        console.error('[durable] task %d marked dead (poison, attempts=%d)', t.id, nextAttempts);
      } else {
        // 复活：attempts+1、回到 pending、清掉旧 heartbeat 让本轮重新接管
        db.prepare(
          `UPDATE tasks
              SET status='pending', attempts=?, heartbeat_at=NULL, updated_at=?
            WHERE id=?`
        ).run(nextAttempts, now, t.id);
        revived.push(t.id);
      }
    }
    return revived;
  });
}

// =====================================================================
// 5. timers 触发原语
//
// dueTimers: 只读，返回 pending 且 fire_at<=now 的 timers（payload 已 parse），按 fire_at 升序。
//   不改状态（由 worker 触发后调 fireTimer 改），读写分离便于 worker 内 try/catch 隔离。
// fireTimer: tx 内把 timer 状态置 'fired'。policy 语义（skip/once 迟到处理）由 worker 决定，
//   本函数只做状态落库——职责单一。
// =====================================================================
export function dueTimers(now = nowMs()) {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT id, fire_at, task_id, payload, catchup_policy
         FROM timers
        WHERE status='pending' AND fire_at <= ?
        ORDER BY fire_at ASC`
    )
    .all(now);
  return rows.map((r) => ({
    id: r.id,
    fire_at: r.fire_at,
    task_id: r.task_id,
    payload: unj(r.payload),
    catchup_policy: r.catchup_policy,
  }));
}

export function fireTimer(timerId /*, policy */) {
  // policy 参数保留以契合契约签名；状态落库对两种 policy 相同（都置 'fired' 一次）。
  // skip vs once 的"补跑几次"语义在 worker 层用 fire_at 距 now 判定，本函数只标记。
  const now = nowMs();
  tx((db) => {
    db.prepare(`UPDATE timers SET status='fired' WHERE id=? AND status='pending'`).run(timerId);
  });
  return now; // 返回触发时刻，便于调用方记录（无副作用语义）
}

// =====================================================================
// 6. 任务状态终结 —— 原子状态变更（致命纪律：单条 UPDATE 同时改 status+result+updated_at）。
// =====================================================================
export function markTaskDone(taskId, result) {
  const now = nowMs();
  tx((db) => {
    db.prepare(
      `UPDATE tasks SET status='done', result=?, last_error=NULL, heartbeat_at=NULL, updated_at=?
        WHERE id=?`
    ).run(j(result), now, taskId);
  });
}

export function markTaskFailed(taskId, errMessage, { dead = false } = {}) {
  const now = nowMs();
  tx((db) => {
    // 错误留证据不吞：last_error 写下来。dead=true 时直接进死信状态（毒任务/不可恢复）。
    db.prepare(
      `UPDATE tasks SET status=?, last_error=?, heartbeat_at=NULL, updated_at=?
        WHERE id=?`
    ).run(dead ? 'dead' : 'failed', String(errMessage ?? 'unknown error'), now, taskId);
  });
}

// running 中的任务每 tick UPSERT heartbeat_at（单条 UPDATE，不开长事务）。
// 为什么也写 status='running'：被 recover 捡回的 pending 任务一旦开始跑就该转 running，
// 否则下一轮 recover 会重复捡（pending 也在扫描范围内）。
export function beatTask(taskId, progress = '') {
  const now = nowMs();
  tx((db) => {
    db.prepare(
      `UPDATE tasks SET status='running', heartbeat_at=?, updated_at=? WHERE id=? AND status IN ('pending','running')`
    ).run(now, now, taskId);
  });
}

// 全局 heartbeat（id=1）UPSERT —— watchdog 读这行判主进程存活。worker 每 tick 调一次。
export function beatHeartbeat(progress = '') {
  const now = nowMs();
  tx((db) => {
    db.prepare(
      `INSERT INTO heartbeat (id, ts, progress) VALUES (1, ?, ?)
       ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, progress=excluded.progress`
    ).run(now, String(progress));
  });
}

// =====================================================================
// 7. worker tick + startWorker
//
// 每 5s 一 tick，顺序固定（每步 try/catch 隔离，单步失败不拖垮整 tick）：
//   ① recoverStaleTasks —— 把崩溃残留的陈旧 task 捡回（attempts+1 / 标 dead）
//   ② dueTimers → 按 catchup_policy 触发（'skip' 迟到只跑下一次 / 'once' 补跑一次）→ fireTimer
//   ③ relayOutbox —— 把 outbox pending 真发出去（fetch 在 tx 外，致命纪律②③）
//   ④ beatHeartbeat —— UPSERT 全局心跳，供进程外 watchdog 判活
//
// 依赖注入（hooks）：为保持 db ← durable 的单向依赖（durable 不该反向 import loop/adapter），
//   relayOutbox / runTask 由 main.mjs 在 startWorker 时注入。无注入时跳过对应步骤（仍可独立 selftest）。
// =====================================================================

// catchup 语义阈值：timer 迟到超过此值，'skip' 策略视为"已错过、不补跑"，只标记 fired。
// 选 1 个 tick 周期：worker 正常运行时不会迟到这么多，迟到这么多 = 进程曾经停过。
const CATCHUP_LATE_MS = TICK_MS * 2;

/**
 * 单次 tick。导出便于 selftest 手动驱动（不必真等 setInterval）。
 * hooks: { relayOutbox?: async ()=>{sent,failed}, runTask?: async (task)=>void, onTimer?: (timer)=>void }
 *   - relayOutbox / runTask 是 async（含 fetch/LLM），故 tick 整体 async；但它们在 tx 外执行。
 *   - onTimer 为同步回调：worker 决定触发某 timer 时调用（派 task 或入 outbox），由 main 注入。
 */
export async function tick(hooks = {}) {
  const { relayOutbox = null, runTask = null, onTimer = null } = hooks;
  const now = nowMs();
  let recovered = 0;
  let firedTimers = 0;
  let relayed = { sent: 0, failed: 0 };

  // ① 崩溃恢复：捡回陈旧 task。重放/重跑本身交给注入的 runTask（在 tx 外，含 LLM）。
  try {
    const revived = recoverStaleTasks();
    recovered = revived.length;
    if (runTask) {
      for (const taskId of revived) {
        const db = getDb();
        const task = db.prepare('SELECT * FROM tasks WHERE id=?').get(taskId);
        if (task) {
          // runTask 是 async（agentic 含 LLM）——在 tx 外 await，绝不占 DB 锁（致命纪律③）。
          // 单个任务失败不能拖垮 tick：各自 try/catch。
          try {
            await runTask({ ...task, payload: unj(task.payload) });
          } catch (e) {
            console.error('[durable] runTask(%d) failed: %s', taskId, e.message);
            markTaskFailed(taskId, e.message);
          }
        }
      }
    }
  } catch (e) {
    console.error('[durable] tick step① recoverStaleTasks failed: %s', e.message);
  }

  // ② 扫 timers，按 catchup_policy 触发。
  try {
    const due = dueTimers(now);
    for (const timer of due) {
      try {
        const lateMs = now - timer.fire_at;
        const skipBecauseLate = timer.catchup_policy === 'skip' && lateMs > CATCHUP_LATE_MS;
        if (skipBecauseLate) {
          // 'skip' 且迟到很多：进程曾停机，这一次错过的不补，只标记 fired（下一次正常触发）。
          // 例：每日 22:30 提醒，关机 3 天，回来只标记不补发 3 条。
          fireTimer(timer.id, timer.catchup_policy);
          console.warn(
            '[durable] timer %d skipped (late %dms, policy=skip)',
            timer.id,
            lateMs
          );
        } else {
          // 'once'（补跑一次）或 'skip' 但没迟到：正常触发一次。
          // onTimer 同步回调：派 task / 入 outbox（由 main 注入）。触发后标记 fired（恰好一次）。
          if (onTimer) {
            try {
              onTimer(timer);
            } catch (e) {
              console.error('[durable] onTimer(%d) failed: %s', timer.id, e.message);
            }
          }
          fireTimer(timer.id, timer.catchup_policy);
          firedTimers++;
        }
      } catch (e) {
        console.error('[durable] tick step② timer %d failed: %s', timer.id, e.message);
      }
    }
  } catch (e) {
    console.error('[durable] tick step② dueTimers failed: %s', e.message);
  }

  // ③ relay outbox（真发在 tx 外，致命纪律②③：带 timeout，不占 DB 锁）。
  try {
    if (relayOutbox) {
      relayed = (await relayOutbox()) || { sent: 0, failed: 0 };
    }
  } catch (e) {
    console.error('[durable] tick step③ relayOutbox failed: %s', e.message);
  }

  // ④ 全局心跳。即使前面有步失败也要打，watchdog 靠它判主进程没死。
  const progress = `tick: recover=${recovered} timers=${firedTimers} sent=${relayed.sent} failed=${relayed.failed}`;
  try {
    beatHeartbeat(progress);
  } catch (e) {
    console.error('[durable] tick step④ heartbeat failed: %s', e.message);
  }

  return { recovered, firedTimers, relayed, progress };
}

let _interval = null;

/**
 * 启动 worker：每 TICK_MS 跑一次 tick。
 * hooks 由 main.mjs 注入（relayOutbox / runTask / onTimer），保持单向依赖。
 * 防重入：上一个 tick 的 async 还没跑完就不开下一个（避免同步 DB 写叠加 + LLM 长任务重入）。
 */
export function startWorker(hooks = {}) {
  // 确保 db 就绪（initDb 幂等）。startWorker 是 durable 子系统的对外启动入口。
  getDb();
  if (_interval) {
    console.warn('[durable] startWorker called twice; ignoring (worker already running)');
    return;
  }
  let running = false;
  _interval = setInterval(async () => {
    if (running) {
      // 上一 tick 还没结束（可能某 runTask 的 LLM 调用很慢）。跳过本次，不堆叠。
      console.warn('[durable] tick overlap, skipping (previous tick still running)');
      return;
    }
    running = true;
    try {
      await tick(hooks);
    } catch (e) {
      // tick 内部已逐步 try/catch；这里是最后兜底，绝不让 setInterval 回调抛穿导致 worker 静默。
      console.error('[durable] tick top-level failure: %s', e.stack || e.message);
    } finally {
      running = false;
    }
  }, TICK_MS);
  // 让进程可以在没有其它 handle 时正常退出（systemd 重启场景）；生产中有 HTTP server 撑着不受影响。
  if (_interval.unref) _interval.unref();
  console.log('[durable] worker started, tick=%dms', TICK_MS);
}

export function stopWorker() {
  if (_interval) {
    clearInterval(_interval);
    _interval = null;
  }
}

// =====================================================================
// --selftest：离线、不联网。建临时 db，验证 durable 全部原语 + 一次 mock tick。
// 运行：node durable.mjs --selftest
// =====================================================================
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { rmSync } from 'node:fs';
import { __setClockForTest, __closeForTest } from './db.mjs';

async function runSelftest() {
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

  const tmpPath = join(tmpdir(), `xw2-durable-selftest-${process.pid}-${Date.now()}.db`);
  process.env.XW2_DB_PATH = tmpPath;

  // 可控时钟：把"现在"钉死，便于断言 timer 触发 / 陈旧判定。
  let T = 1_700_000_000_000; // 任意基准 epoch ms
  __setClockForTest(() => T);

  try {
    const db = getDb(); // 触发 initDb（XW2_DB_PATH 已指临时文件）
    ok(!!db, 'db 就绪');

    // --- dedupHash 稳定性 ---
    const h1 = dedupHash(['wecom', 'owner', 'hi', '']);
    const h2 = dedupHash(['wecom', 'owner', 'hi', '']);
    const h3 = dedupHash(['wecom', 'owner', 'HI', '']);
    ok(h1 === h2 && h1.length === 16, 'dedupHash 确定性 + 16 hex');
    ok(h1 !== h3, 'dedupHash 内容不同 → hash 不同');

    // --- createTask 原子提交 + businessFn 同事务 ---
    const r1 = createTask(
      { kind: 'agentic', payload: { goal: 'remind' }, idempotencyKey: 'idem-A' },
      (d) => {
        // 业务写入：记一条 episode，必须和 task 同事务原子提交
        d.prepare('INSERT INTO episodes (ts, role, content) VALUES (?,?,?)').run(
          T,
          'system',
          'task created'
        );
      }
    );
    ok(r1.taskId > 0 && r1.deduped === false, 'createTask 建任务返回 taskId');
    const epiCount = db.prepare('SELECT COUNT(*) c FROM episodes').get().c;
    ok(epiCount === 1, 'businessFn 与 task 同事务原子提交（episode 落库）');

    // --- idempotency 去重 ---
    const r2 = createTask({ kind: 'agentic', payload: { goal: 'remind' }, idempotencyKey: 'idem-A' });
    ok(r2.taskId === r1.taskId && r2.deduped === true, 'idempotency_key 命中 → 去重不重复建');
    const taskTotal = db.prepare('SELECT COUNT(*) c FROM tasks').get().c;
    ok(taskTotal === 1, '去重后任务总数仍为 1');

    // businessFn 不应在去重路径执行（否则会重复业务写入）
    const r2b = createTask({ kind: 'agentic', idempotencyKey: 'idem-A' }, (d) => {
      d.prepare('INSERT INTO episodes (ts, role, content) VALUES (?,?,?)').run(T, 'system', 'should-not-run');
    });
    ok(r2b.deduped === true, '去重路径命中');
    ok(db.prepare('SELECT COUNT(*) c FROM episodes').get().c === 1, '去重时 businessFn 不执行（无重复业务写入）');

    // --- step memo：getStepMemo / recordStep / markComplete 别名 ---
    ok(getStepMemo(r1.taskId, 0) === undefined, '未记录的 step → getStepMemo 返 undefined');
    recordStep(r1.taskId, 0, { toolName: 'send', params: { x: 1 }, returnValue: { sent: true } });
    const memo = getStepMemo(r1.taskId, 0);
    ok(memo && memo.sent === true, 'recordStep 后 getStepMemo 返存值（重放跳过用）');
    markComplete(r1.taskId, 1, { toolName: 'x', returnValue: 42 }); // 别名等同 recordStep
    ok(getStepMemo(r1.taskId, 1) === 42, 'markComplete 别名落 memo 成功');

    // --- scheduleTimer + dueTimers + catchup ---
    const tmrOnce = scheduleTimer({ fireAt: T + 1000, payload: { kind: 'remind' }, catchupPolicy: 'once' });
    const tmrSkip = scheduleTimer({ fireAt: T + 1000, catchupPolicy: 'skip' });
    ok(tmrOnce > 0 && tmrSkip > 0, 'scheduleTimer 返回 timer id');
    ok(dueTimers(T).length === 0, '未到期 timer 不在 dueTimers');
    // 推进时钟到刚过期
    T += 2000;
    const due = dueTimers(T);
    ok(due.length === 2, '到期后 dueTimers 返回 2 条');
    ok(due[0].payload && typeof due[0].payload === 'object', 'dueTimers payload 已 JSON.parse');

    // fireTimer 标记 fired
    fireTimer(tmrOnce, 'once');
    ok(dueTimers(T).length === 1, 'fireTimer 后该 timer 退出 due 集合');

    // --- markTaskDone / markTaskFailed 原子状态变更 ---
    markTaskDone(r1.taskId, { reply: 'ok' });
    const doneRow = db.prepare('SELECT status, result FROM tasks WHERE id=?').get(r1.taskId);
    ok(doneRow.status === 'done' && unj(doneRow.result).reply === 'ok', 'markTaskDone 原子改 status+result');

    const r3 = createTask({ kind: 'deterministic', payload: {} });
    markTaskFailed(r3.taskId, 'kaboom');
    const failRow = db.prepare('SELECT status, last_error FROM tasks WHERE id=?').get(r3.taskId);
    ok(failRow.status === 'failed' && failRow.last_error === 'kaboom', 'markTaskFailed 留证据 last_error');

    // --- beatTask：pending → running + heartbeat ---
    const r4 = createTask({ kind: 'agentic', payload: {} });
    beatTask(r4.taskId, 'working');
    const beatRow = db.prepare('SELECT status, heartbeat_at FROM tasks WHERE id=?').get(r4.taskId);
    ok(beatRow.status === 'running' && beatRow.heartbeat_at === T, 'beatTask 置 running + 写 heartbeat_at');

    // --- recoverStaleTasks：陈旧 running 被捡回 ---
    // r4 现在是 running，heartbeat_at=T。推进时钟超过 STALE_TASK_MS。
    T += STALE_TASK_MS + 1000;
    const revived = recoverStaleTasks();
    ok(revived.includes(r4.taskId), 'recoverStaleTasks 捡回陈旧 running 任务');
    const revRow = db.prepare('SELECT status, attempts FROM tasks WHERE id=?').get(r4.taskId);
    ok(revRow.status === 'pending' && revRow.attempts === 1, '复活任务回 pending + attempts+1');

    // --- 毒任务：attempts 超限 → dead，不返回 ---
    const r5 = createTask({ kind: 'agentic', payload: {} });
    // 直接把 attempts 拉到上限，并制造陈旧
    db.prepare('UPDATE tasks SET attempts=?, status=?, heartbeat_at=? WHERE id=?').run(
      MAX_TASK_ATTEMPTS,
      'running',
      T - STALE_TASK_MS - 5000,
      r5.taskId
    );
    const revived2 = recoverStaleTasks();
    ok(!revived2.includes(r5.taskId), '超限毒任务不被复活返回');
    const deadRow = db.prepare('SELECT status FROM tasks WHERE id=?').get(r5.taskId);
    ok(deadRow.status === 'dead', '毒任务标记为 dead');

    // --- catchup 'skip' 迟到跳过 vs 'once' 补跑 ---
    // 新建一个 skip timer，fire_at 远早于 now（模拟关机后回来）
    const tmrLate = scheduleTimer({ fireAt: T - (CATCHUP_LATE_MS + 10000), catchupPolicy: 'skip' });
    const tmrOnceLate = scheduleTimer({ fireAt: T - (CATCHUP_LATE_MS + 10000), catchupPolicy: 'once' });
    let onTimerFired = [];
    await tick({ onTimer: (t) => onTimerFired.push(t.id) });
    // skip 迟到 → 只标记，不进 onTimer；once 迟到 → 补跑一次进 onTimer
    ok(!onTimerFired.includes(tmrLate), "catchup 'skip' 迟到 → 不补发（onTimer 未触发）");
    ok(onTimerFired.includes(tmrOnceLate), "catchup 'once' 迟到 → 补跑一次（onTimer 触发）");
    const lateStatus = db.prepare('SELECT status FROM timers WHERE id=?').get(tmrLate).status;
    ok(lateStatus === 'fired', "'skip' 迟到 timer 已标记 fired（不会反复扫）");

    // --- tick 注入 relayOutbox（mock，验证在 tx 外 await 不崩）---
    let relayCalled = false;
    const res = await tick({
      relayOutbox: async () => {
        relayCalled = true;
        return { sent: 2, failed: 0 };
      },
    });
    ok(relayCalled && res.relayed.sent === 2, 'tick 调用注入的 relayOutbox（async，tx 外）');

    // --- tick 写全局 heartbeat ---
    const hb = db.prepare('SELECT ts, progress FROM heartbeat WHERE id=1').get();
    ok(hb.ts === T && /tick:/.test(hb.progress), 'tick 末尾 UPSERT 全局 heartbeat');

    // --- tick 单步失败隔离：runTask 抛错不拖垮 tick，任务标 failed ---
    const r6 = createTask({ kind: 'agentic', payload: {} });
    db.prepare('UPDATE tasks SET status=?, heartbeat_at=? WHERE id=?').run(
      'running',
      T - STALE_TASK_MS - 5000,
      r6.taskId
    );
    const res2 = await tick({
      runTask: async () => {
        throw new Error('runTask boom');
      },
    });
    ok(res2.progress.includes('tick:'), 'runTask 抛错被隔离，tick 仍完成并打 heartbeat');
    const r6Status = db.prepare('SELECT status FROM tasks WHERE id=?').get(r6.taskId).status;
    ok(r6Status === 'failed', 'runTask 失败的任务被 markTaskFailed');
  } catch (e) {
    fail++;
    console.log('  ✗ selftest 异常: ' + e.stack);
  } finally {
    __setClockForTest(null);
    __closeForTest();
    for (const ext of ['', '-wal', '-shm']) {
      try {
        rmSync(tmpPath + ext, { force: true });
      } catch (e) {
        /* ignore */
      }
    }
  }

  console.log(`\n[durable.mjs selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href && process.argv.includes('--selftest')) {
  runSelftest();
}
