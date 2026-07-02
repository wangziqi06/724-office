// =====================================================================
// main.mjs —— 进程编排入口（worker tick + 入站 + 优雅退出）
//
// 职责（契约 §模块接口契约 main.mjs）：
//   - startWorker()      : initDb → setInterval(tick, TICK_MS) 每 tick 顺序：
//                          ① recoverStaleTasks 重放陈旧 task
//                          ② dueTimers → 按 catchup_policy 触发 → fireTimer
//                          ③ relayOutbox()
//                          ④ UPSERT heartbeat(id=1)
//                          每步 try/catch 隔离，单步失败不拖垮整 tick（留证据）。
//   - handleIncoming()   : appendEpisode(user) → runAgentic → appendEpisode(assistant) → reply 文本。
//   - CLI 入口           : --selftest 离线全链路；否则 startWorker() + startAdapter(wecom/cli)。
//
// 编排：initDb → 注册工具（tools 已是静态注册表，import 即注册）→ startWorker → startAdapter。
// 优雅退出：SIGTERM/SIGINT → 停 worker interval + adapter.stop() + 最后一次 flush + 关 db。
//
// 依赖方向（契约）：db ← durable/memory/adapter ← tools ← loop ← main。main 在最顶层，可 import 全部。
// =====================================================================

import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import {
  initDb,
  getDb,
  tx,
  nowMs,
  TICK_MS,
} from './db.mjs';
import {
  recoverStaleTasks,
  dueTimers,
  fireTimer,
  createTask,
  beatTask,
  beatHeartbeat,
  markTaskDone,
  markTaskFailed,
} from './durable.mjs';
import { appendEpisode } from './memory.mjs';
import { relayOutbox, enqueueOutbox, startAdapter } from './adapter.mjs';
import { runAgentic } from './loop.mjs';
import { assembleContext } from './context.mjs';
import { initEsmSchema, esmDuePrompt, markEsmSent } from './esm.mjs';
import { initRecurringSchema, dueRecurringJobs, markJobFired } from './recurring.mjs';
import { morningBriefing, eveningBriefing, weatherBriefing } from './weather.mjs';
import { IDENTITY } from './identity.mjs';

// 单 tick 最多执行的到期任务数：限制一拍跑太久（每个任务可能跑完整 agentic 循环）拖垮心跳/relay。
// 余下的到期任务下一拍继续，n=1 同分钟堆多个提醒极罕见。
const MAX_DUE_TASKS_PER_TICK = 3;

// ---- worker tick 内的常量。TICK_MS 来自 db.mjs（常量集中，契约 §常量集中）。----
// 若 db.mjs 未导出 TICK_MS（防御性），本地兜底 5000。
const TICK_INTERVAL_MS = Number(TICK_MS || 5000);

// worker interval 句柄，模块级持有以便优雅退出 clearInterval。
let _tickTimer = null;
let _ticking = false; // 防重入：上一 tick 还在 await（relayOutbox 慢）时跳过本 tick

// ---------------------------------------------------------------------
// 单次 tick：四步顺序 + 每步 try/catch 隔离（致命纪律：失败要响不静默吞）。
// 为什么每步独立 try/catch：某步抛错（如某 timer payload 坏）不能拖垮 heartbeat 更新，
// 否则 watchdog 会误判进程死。heartbeat 一定要写到（最后一步）。
// ---------------------------------------------------------------------
async function tick() {
  if (_ticking) return; // 上一 tick 的 await 未完成，跳过（避免堆叠）
  _ticking = true;
  const db = getDb();
  let progress = '';
  let dueRan = 0;
  let recovered = 0;
  let firedTimers = 0;
  let recurringFired = 0;
  let relayResult = { sent: 0, failed: 0 };

  // ⓪ 到期任务执行（durable 超能力的真正落地）：扫 pending 且 next_run_at<=now 的 task，
  //   原子认领后跑完整 agentic 循环（在 tx 外，含 LLM），完成后送达 + markDone。
  //   放在 recover 之前：先认领到期任务（→running），recover 就不会把它们当陈旧重复捡。
  //   单步 try/catch 隔离：某个任务炸不拖垮 relay/heartbeat。
  try {
    dueRan = await runDueTasks();
  } catch (e) {
    console.error('[worker] step0 runDueTasks failed: %s', e.message);
  }

  // ① 崩溃恢复：把卡在 running 且心跳陈旧的 task（进程跑一半崩了）退回 pending，下一拍由 runDueTasks 重跑；
  //   毒任务（attempts 超限）置 dead。pending+到期但本拍没被 runDueTasks 认领的也会被退回（无害，下拍再跑）。
  try {
    const staleIds = recoverStaleTasks();
    recovered = staleIds.length;
  } catch (e) {
    console.error('[worker] step1 recoverStaleTasks failed: %s', e.message);
  }

  // ② 到期 timer：按 catchup_policy 触发（派 task 或入 outbox）→ fireTimer 标 fired。
  try {
    const due = dueTimers();
    for (const t of due) {
      try {
        await handleDueTimer(t);
        fireTimer(t.id, t.catchup_policy);
        firedTimers++;
      } catch (e) {
        // 单个 timer 失败不影响其它 timer（隔离）。
        console.error('[worker] timer id=%s fire failed: %s', t.id, e.message);
      }
    }
  } catch (e) {
    console.error('[worker] step2 dueTimers failed: %s', e.message);
  }

  // ②.5 ESM 排程：到点(晨08:30/晚22:30/周日回顾)入 outbox + 设 pending。确定性、便宜，每拍查；同 tick 由 ③ relay 发出。
  try {
    const esmDue = esmDuePrompt(getDb());
    if (esmDue && esmDue.content) {
      const target = process.env.WECOM_TARGET_ID || process.env.OWNER_ID || '';
      if (target) {
        const norm = ['wecom', String(target), String(esmDue.content), String(esmDue.dedupTag || '')]
          .map((p) => String(p ?? '').trim()).join(' ');
        const dedupHash = createHash('sha256').update(norm).digest('hex').slice(0, 16);
        enqueueOutbox({ channel: 'wecom', target, content: esmDue.content, dedupHash });
        markEsmSent(getDb(), esmDue); // 入队成功后才落"已发"标志+pending（enqueue 抛错则不落标，下拍重试，dedup 兜底）
        console.error('[esm] due %s enqueued', esmDue.kind === 'review' ? 'weekly-review' : esmDue.type);
      } else {
        markEsmSent(getDb(), esmDue); // 无 target=配置缺失：标记已发避免每拍重复刷日志（这天放弃，等修配置）
        console.error('[esm] due prompt but no target set (WECOM_TARGET_ID/OWNER_ID)');
      }
    }
  } catch (e) {
    console.error('[worker] step esm failed: %s', e.message);
  }

  // ②.6 周期任务（恢复自旧小王的核心机制）：到点的 recurring job → 执行。
  //   builtin(天气)直接拼文案入 outbox（确定性、无 AI 味）；agentic 派 task 走 runDueTasks。
  //   每拍查、便宜；动作成功后才落标，当天不重触发（与 ESM 同纪律，失败下拍在补发窗内重试）。
  try {
    const target = process.env.WECOM_TARGET_ID || process.env.OWNER_ID || '';
    for (const job of dueRecurringJobs(getDb())) {
      try {
        if (job.kind === 'builtin') {
          const text = await runRecurringBuiltin(job.action); // async（天气取数在 tx 外）
          if (text && target) {
            const norm = ['wecom', String(target), String(text), `recurring:${job.id}:${job._date}`].map((p) => String(p ?? '').trim()).join(' ');
            const dedupHash = createHash('sha256').update(norm).digest('hex').slice(0, 16);
            enqueueOutbox({ channel: 'wecom', target, content: text, dedupHash });
          }
        } else {
          // agentic：派一个到期任务，由 runDueTasks 跑 message（小王到点自己做事）。
          const idem = createHash('sha256').update(`recurring ${job.id} ${job._date}`).digest('hex').slice(0, 16);
          createTask({ kind: 'agentic', payload: { note: job.action.message, target }, idempotencyKey: idem, nextRunAt: nowMs() });
        }
        markJobFired(getDb(), job.id, job._date);
        recurringFired++;
        console.error('[recurring] fired job %d (%s)', job.id, job.name);
      } catch (e) {
        console.error('[recurring] job %d (%s) 执行失败: %s', job.id, job.name, e.message);
      }
    }
  } catch (e) {
    console.error('[worker] step recurring failed: %s', e.message);
  }

  // ③ relay outbox：扫 pending → 真发 → 标 sent（fetch 在 tx 外，致命纪律③）。
  try {
    relayResult = await relayOutbox();
  } catch (e) {
    console.error('[worker] step3 relayOutbox failed: %s', e.message);
  }

  // ④ UPSERT heartbeat（watchdog 读这行判存活，必须写到）。
  // 单条 UPSERT，不开长事务（契约 beatTask 同理：不占 DB 锁）。
  progress = `tick: ${dueRan} ran, ${recovered} recovered, ${firedTimers} timers, ${recurringFired} recurring, relay ${relayResult.sent}/${relayResult.failed}`;
  try {
    db.prepare(
      `INSERT INTO heartbeat (id, ts, progress) VALUES (1, ?, ?)
       ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, progress = excluded.progress`
    ).run(nowMs(), progress);
  } catch (e) {
    console.error('[worker] step4 heartbeat upsert failed: %s', e.message);
  }

  _ticking = false;
}

// 到期 timer 的语义层（契约 dueTimers/fireTimer 只做状态，语义在 worker）：
//   - 有 task_id：唤醒/派该 task（这里用 createTask 幂等派一个 agentic 提醒任务；
//     若 timer 已绑定既有 task，则不重复派，仅由 fireTimer 标记）。
//   - 无 task_id：纯提醒 → 直接入 outbox 发给 owner。
//   - catchup_policy='skip'：迟到的 timer 仅标记不补发（fireTimer 已处理状态，这里发送逻辑也据此跳过补发）。
async function handleDueTimer(t) {
  const payload = t.payload && typeof t.payload === 'object' ? t.payload : {};

  if (t.task_id) {
    // 已绑定 task：交给 task 重跑链路，这里不重复派业务，仅记录。
    console.error('[worker] timer id=%s wakes task_id=%s', t.id, t.task_id);
    return;
  }

  // 纯提醒：入 outbox 直发。dedup_hash 按 conventions：[channel,target,content,taskId??'']。
  const target = payload.target || process.env.WECOM_TARGET_ID || '';
  const content = payload.content || payload.message || '（定时提醒）';
  if (!target) {
    console.error('[worker] timer id=%s has no target, skip outbox', t.id);
    return;
  }
  // taskId 用 timer id 参与 hash，保证不同 timer 的同文案也能各发一次；同一 timer 重触发则去重。
  const norm = ['wecom', String(target), String(content), `timer:${t.id}`].map((p) => String(p ?? '').trim()).join(' ');
  const dedupHash = createHash('sha256').update(norm).digest('hex').slice(0, 16);
  enqueueOutbox({ channel: 'wecom', target, content, dedupHash });
}

// =====================================================================
// 到期任务执行 —— durable「跨天/长任务真的会跑并送达」的落地处。
//
// 为什么需要它（修复的 bug）：schedule_task 工具会 createTask(agentic, next_run_at=fireAt)，
//   但旧 worker 从不执行任何 task —— timer 触发只 console.error 记录、recover 只统计数量。
//   结果：子淇让小王「3 天后提醒我」，小王说「好」，但那条提醒永远不会到（task 几拍后被标 dead）。
//   现在由 runDueTasks 真正驱动执行：扫到期 pending task → 认领 → 跑 agentic → 结构性兜底送达。
// =====================================================================

// 原子认领：pending→running（单条 UPDATE，tx 内）。changes=1 表示本进程成功认领；
// 0 表示已被别处认领/状态已变（防与 recover、并发入站重复执行同一任务）。
function claimTask(taskId) {
  const now = nowMs();
  return tx((db) => {
    const info = db
      .prepare(`UPDATE tasks SET status='running', heartbeat_at=?, updated_at=? WHERE id=? AND status='pending'`)
      .run(now, now, taskId);
    return info.changes === 1;
  });
}

// 安全 JSON.parse（payload 坏掉不拖垮任务执行；留证据）。
function safeParse(s) {
  if (s == null) return {};
  if (typeof s === 'object') return s;
  try {
    return JSON.parse(s) || {};
  } catch (e) {
    console.error('[worker] task payload JSON 解析失败，按空处理: %s', e.message);
    return {};
  }
}

// 扫描到期待跑任务并逐个执行。返回成功跑完的任务数。
async function runDueTasks() {
  const db = getDb();
  const due = db
    .prepare(
      `SELECT id, kind, payload, attempts FROM tasks
        WHERE status='pending' AND next_run_at IS NOT NULL AND next_run_at <= ?
        ORDER BY next_run_at ASC
        LIMIT ?`,
    )
    .all(nowMs(), MAX_DUE_TASKS_PER_TICK);

  let ran = 0;
  for (const t of due) {
    if (!claimTask(t.id)) continue; // 没认领到（已被处理）→跳过
    try {
      await runScheduledTask({ ...t, payload: safeParse(t.payload) });
      ran++;
    } catch (e) {
      // 任务级失败不拖垮整 tick：标 failed 留证据。recover 不会重捡 failed（终态），
      // 但下面 runScheduledTask 已对"LLM 故障"做了兜底投递，必达性不依赖这里。
      console.error('[worker] runScheduledTask(%d) failed: %s', t.id, e.message);
      try { markTaskFailed(t.id, e.message); } catch (e2) { console.error('[worker] markTaskFailed(%d) failed: %s', t.id, e2.message); }
    }
    // 每跑完一个任务打一次全局心跳：连续多个长任务时避免把 tick 拖过 watchdog 阈值被误判死。
    try { beatHeartbeat(`ran scheduled task ${t.id}`); } catch (e) { /* 心跳失败下一拍补 */ }
  }
  return ran;
}

// 执行单个到期任务：组上下文 → 跑 agentic 循环（小王到点自己决定做什么/发什么）→
//   结构性兜底（原则2：结构保证不靠 prompt）：本次跑若没产生任何 outbox 行，就把提醒补发出去，
//   保证「定时提醒必达」即使模型忘了调 send_message、或 LLM 整个挂了。
async function runScheduledTask(task) {
  const payload = task.payload || {};
  const note = String(payload.note ?? payload.content ?? payload.message ?? '').trim();
  // schedule_task 把 payload.target 存成 ctx.sessionId='wecom:<id>'（带前缀）。发送给企微的 toId 要裸 id，
  // 故剥掉 'wecom:' 前缀再用；缺失则回落 owner。sessionId 仍用带前缀的会话格式（喂 runAgentic / 召回隔离）。
  const rawTarget = String(payload.target || '');
  const target = rawTarget.replace(/^wecom:/, '').trim() || process.env.WECOM_TARGET_ID || process.env.OWNER_ID || '';
  const sessionId = rawTarget.startsWith('wecom:') ? rawTarget : (target ? `wecom:${target}` : 'scheduled');
  const db = getDb();

  // 记下执行前 outbox 上界 id：用于判断本次跑有没有真的发出任何消息（兜底投递的依据）。
  const beforeMax = db.prepare(`SELECT COALESCE(MAX(id),0) m FROM outbox`).get().m;

  const framing =
    `〔定时任务到点〕这是你之前为${IDENTITY.ownerName}排好的任务，现在到时间了，去把它做掉：\n「${note}」\n` +
    `多数情况下你只需把要提醒/告知${IDENTITY.ownerName}的话用 send_message 工具发给他（target=${target}）。` +
    `若任务需要先查点信息再说，可以调用工具。完成后用一句话回复即可，不要在正文里假装已发。`;

  let reply = '';
  try {
    const ctx = assembleContext(sessionId, note || framing);
    const out = await runAgentic({
      taskId: task.id,
      sessionId,
      userInput: framing,
      history: ctx.recentTurns,
      summary: ctx.summary,
      anchors: ctx.anchors,
      recalled: ctx.recalled,
      recallWeak: ctx.recallWeak,
      pendingCheckin: null, // 定时任务不是打卡回复，不提供 record_checkin
      sinceLastMs: ctx.sinceLastMs, // 定时任务同样带时间感（"他半天没说话了"是有效背景）
    });
    reply = out && out.reply != null ? String(out.reply) : '';
    // 记一条 assistant episode：让"我到点提醒过你 X"这件事进记忆，将来可召回。
    try {
      appendEpisode({ sessionId, role: 'assistant', content: `〔定时任务#${task.id}〕${reply}`, taskId: task.id });
    } catch (e) { console.error('[worker] task %d episode 落库失败(忽略): %s', task.id, e.message); }
  } catch (e) {
    // 不抛：交给下面结构性兜底投递，保证提醒不因 LLM 故障彻底丢失。
    console.error('[worker] scheduled task %d agentic run failed: %s', task.id, e.message);
  }

  // 结构性兜底投递：本次跑没新增任何 outbox 行（说明 agent 没发消息或整个挂了）→ 把提醒补发出去。
  const afterMax = db.prepare(`SELECT COALESCE(MAX(id),0) m FROM outbox`).get().m;
  if (afterMax === beforeMax && target) {
    const body = reply && reply.trim()
      ? reply.trim()
      : (note ? `提醒你：${note}` : '（你之前让我到点提醒你一件事，但具体内容我没记全，方便的话再跟我说一次。）');
    const norm = ['wecom', String(target), body, `task:${task.id}`].map((p) => String(p ?? '').trim()).join(' ');
    const dedupHash = createHash('sha256').update(norm).digest('hex').slice(0, 16);
    try {
      enqueueOutbox({ channel: 'wecom', target, content: body, dedupHash });
    } catch (e) { console.error('[worker] task %d 兜底投递失败: %s', task.id, e.message); }
  }

  markTaskDone(task.id, { reply, delivered: true });
}

// builtin 周期动作分发（确定性，不经 agent —— 避免天气这种固定动作被 LLM 加 AI 味旁白）。
// 目前只有天气；未来要加别的 builtin 在这里扩。返回要发给子淇的文案，或 null。
async function runRecurringBuiltin(action) {
  if (action && action.handler === 'weather') {
    if (action.preset === 'morning') return await morningBriefing();
    if (action.preset === 'evening') return await eveningBriefing();
    return await weatherBriefing(action.params || {});
  }
  console.error('[recurring] 未知 builtin handler: %s', action && action.handler);
  return null;
}

// ---------------------------------------------------------------------
// startWorker() —— 契约签名。initDb → setInterval(tick)。
// 立即跑一次 tick（不等首个 5s 间隔），让 heartbeat 尽快有值（watchdog 友好）。
// ---------------------------------------------------------------------
export function startWorker() {
  initDb();
  initEsmSchema(getDb()); // ESM 表(esm_raw/esm_coded/daily_events) + bot_state，idempotent
  initRecurringSchema(getDb()); // recurring_jobs 表（周期任务：天气播报等），idempotent
  if (_tickTimer) return; // 幂等：已启动不重复
  // 立即一拍，随后周期。
  tick().catch((e) => console.error('[worker] initial tick failed: %s', e.message));
  _tickTimer = setInterval(() => {
    tick().catch((e) => console.error('[worker] tick failed: %s', e.message));
  }, TICK_INTERVAL_MS);
  _tickTimer.unref?.(); // 不阻止进程在其它句柄关闭后退出
  console.error('[worker] started, tick every %dms', TICK_INTERVAL_MS);
}

function stopWorker() {
  if (_tickTimer) {
    clearInterval(_tickTimer);
    _tickTimer = null;
    console.error('[worker] stopped');
  }
}

// ---------------------------------------------------------------------
// handleIncoming({sessionId,userInput,rawUserInput?}) —— 入站消息入口（契约签名）。
// appendEpisode(user) → runAgentic → appendEpisode(assistant) → 返回 reply 文本。
// 对话本身不在 worker tick 里跑（契约：对话与 tick 解耦）。
// rawUserInput：成串消息装配时主人的纯原话（不含时间脚手架/图片描述），用于召回检索词 +
//   record_checkin 等不可逆登记；null=单条直通，与 userInput 相同。
// ---------------------------------------------------------------------
export async function handleIncoming({ sessionId, userInput, rawUserInput = null }) {
  // ① 先组装上下文：此刻 userInput 尚未入库 → recentTurns 不含它；userInput 只在 loop 末尾注入一次（杜绝双注入）。
  //    召回检索词用纯原话（装配脚手架稀释 FTS 命中）；纯图轮原话为 '' → 回退用图片描述当检索词（|| 非 ??）。
  const ctx = assembleContext(sessionId, rawUserInput || userInput);

  // ② 再落 user episode（只追加；存模型所见的装配文本，逐字近窗保留"哪条隔多久"的时间事实）。
  appendEpisode({ sessionId, role: 'user', content: String(userInput ?? '') });

  let reply;
  try {
    const out = await runAgentic({
      taskId: null,
      sessionId,
      userInput,
      rawUserInput,
      history: ctx.recentTurns, // 逐字近窗（修 amnesia：以前这里是默认 []）
      summary: ctx.summary,
      anchors: ctx.anchors,
      recalled: ctx.recalled,
      recallWeak: ctx.recallWeak,
      pendingCheckin: ctx.pendingCheckin, // 原则11：有待回打卡时让模型自己判断是否登记（record_checkin）
      sinceLastMs: ctx.sinceLastMs, // 轮间时间事实：进 system「现在」段
    });
    reply = out && out.reply != null ? out.reply : '';
  } catch (e) {
    // 失败要响：记错误 episode + 给用户一个可读回执，不静默吞。
    console.error('[main] runAgentic failed: %s', e.message);
    reply = '（我这会儿卡了一下，稍后再试。）';
  }

  appendEpisode({ sessionId, role: 'assistant', content: String(reply ?? '') });
  return reply;
}

// ---------------------------------------------------------------------
// 优雅退出：停 worker、停 adapter、最后 flush 一次 relay、关 db。
// 为什么最后 flush：进程退出前把已入队但没发的 outbox 尽量发掉（崩溃恢复 + at-least-once 双保险）。
// ---------------------------------------------------------------------
function installGracefulShutdown(adapterHandle) {
  let shuttingDown = false;
  const shutdown = async (sig) => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.error('[main] received %s, shutting down...', sig);
    stopWorker();
    try {
      adapterHandle?.stop?.();
    } catch (e) {
      console.error('[main] adapter stop failed: %s', e.message);
    }
    try {
      await relayOutbox(); // 最后一次 flush
    } catch (e) {
      console.error('[main] final relay failed: %s', e.message);
    }
    // node:sqlite 连接随进程退出自动释放；无显式 close 也安全（WAL 已落盘）。
    console.error('[main] bye.');
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  // 未捕获异常/拒绝：响亮记录，不静默退出（systemd Restart=always 会拉起）。
  process.on('uncaughtException', (e) => console.error('[main] uncaughtException: %s\n%s', e.message, e.stack));
  process.on('unhandledRejection', (e) => console.error('[main] unhandledRejection: %s', e && e.message ? e.message : e));
}

// ---------------------------------------------------------------------
// main() —— 真实启动：注册工具（import 即注册）→ startWorker → startAdapter。
// 模式由 env 决定：有企微凭证（WECOM_TOKEN）→ wecom 回调模式；否则 cli 模式（本地调试）。
// ---------------------------------------------------------------------
async function main() {
  initDb(); // 显式先建库（startWorker 内也会调，幂等）

  startWorker();

  const mode = process.env.WECOM_TOKEN ? 'wecom' : 'cli';
  // 安全 fail-fast（团队原则3：结构强制，不靠运维记得填 .env）：wecom 回调端口对公网开放，
  // 唯一访问控制是 /cb/<secret> 路径密钥。secret 为空 = 任何人 POST 即可触发 agent（烧 token / 借身份发消息）。
  // 这种情况下拒绝启动，把配置错误逼出来，而不是默默以"裸奔"状态上线。
  if (mode === 'wecom' && !process.env.WECOM_CALLBACK_SECRET) {
    console.error('[main] FATAL: wecom 模式但 WECOM_CALLBACK_SECRET 为空 —— 回调端口将无鉴权暴露公网。已拒绝启动，请在 .env 配置 WECOM_CALLBACK_SECRET 后重启。');
    process.exit(1);
  }
  const adapterHandle = startAdapter(mode, {
    onMessage: handleIncoming,
    pollOutbox: relayOutbox,
  });

  installGracefulShutdown(adapterHandle);
  console.error('[main] xiaowang-v2 up. mode=%s', mode);
}

// =====================================================================
// --selftest：离线 mock 全链路（契约 §selftest 必测链路）。
// 不联网（LLM mock + ADAPTER_MOCK）；用临时 db（运行命令设 XW2_DB_PATH 指 scratchpad 临时文件）。
// 验证编排级链路：startWorker tick 跑通、timer 触发入 outbox、handleIncoming 走完一次 agentic、
// outbox dedup 命中只发一次、recoverStaleTasks 捡回陈旧 task。
// =====================================================================
async function selftest() {
  let pass = 0;
  let fail = 0;
  const ok = (cond, msg) => {
    if (cond) { pass++; console.log('  ✓ ' + msg); }
    else { fail++; console.log('  ✗ ' + msg); }
  };

  console.log('main selftest (offline mock)\n');

  initDb();
  initEsmSchema(getDb()); // ESM 表 + bot_state（tick 的 esmDuePrompt 需要）
  initRecurringSchema(getDb()); // recurring_jobs 表（tick 的 dueRecurringJobs 需要）

  // --- 注入 LLM mock（契约 §selftest mock LLM）：脚本化一次 tool_call → send_message，再一轮终止。 ---
  const llm = await import('./llm.mjs');
  let turn = 0;
  if (typeof llm.setMockHandler === 'function') {
    llm.setMockHandler((req) => {
      turn++;
      if (turn === 1) {
        // 第一轮：调 send_message 工具（验证 agentic 循环 + 工具回灌 + outbox 去重）。
        return {
          content: '',
          toolCalls: [
            {
              id: 'call_1',
              type: 'function',
              function: { name: 'send_message', arguments: JSON.stringify({ target: 'owner1', content: '自检消息' }) },
            },
          ],
        };
      }
      // 后续轮：返回最终文本，终止循环。
      return { content: '好的，已记下。', toolCalls: [] };
    });
  }

  // 1) handleIncoming 走完一次 agentic（含 tool_call 回灌）。
  let reply;
  try {
    reply = await handleIncoming({ sessionId: 'selftest', userInput: '提醒我喝水' });
    ok(typeof reply === 'string' && reply.length > 0, 'handleIncoming returns non-empty reply (agentic 循环走通)');
  } catch (e) {
    ok(false, 'handleIncoming threw: ' + e.message);
  }

  // 2) episodes 记了 user + assistant 两条。
  const db = getDb();
  const epCount = db.prepare(`SELECT COUNT(*) c FROM episodes WHERE session_id = 'selftest'`).get().c;
  ok(epCount >= 2, `episodes appended (user+assistant), got ${epCount}`);

  // 3) send_message 工具应已把消息入 outbox（经 wrapper dedup）。
  const obPending = db.prepare(`SELECT COUNT(*) c FROM outbox WHERE status = 'pending'`).get().c;
  ok(obPending >= 1, `outbox has pending message from send_message tool, got ${obPending}`);

  // 4) scheduleTimer + dueTimers 触发：派一个立即到期的纯提醒 timer，跑一拍 tick，应入 outbox。
  const { scheduleTimer } = await import('./durable.mjs');
  scheduleTimer({ fireAt: nowMs() - 1000, payload: { target: 'owner1', content: '定时提醒X' }, catchupPolicy: 'once' });
  await tick(); // 跑一拍：dueTimers → handleDueTimer → enqueueOutbox + relay
  const timerMsg = db.prepare(`SELECT COUNT(*) c FROM outbox WHERE content = '定时提醒X'`).get().c;
  ok(timerMsg === 1, 'due timer enqueued reminder into outbox exactly once');

  // 5) outbox relay：tick 已 relay（ADAPTER_MOCK），SENT 应有内容，且重复 tick 不重发。
  const { SENT } = await import('./adapter.mjs');
  const sentBefore = SENT.length;
  await tick(); // 再跑一拍
  ok(SENT.length === sentBefore, 'second tick does not re-send already-sent outbox (去重+status=sent)');

  // 6) recoverStaleTasks：造一个 heartbeat 陈旧的 running task，应被捡回。
  const { createTask } = await import('./durable.mjs');
  const { taskId } = createTask({ kind: 'agentic', payload: { x: 1 }, idempotencyKey: 'stale-test' });
  // 手动把它置为 running + 陈旧 heartbeat（绕过正常流程，纯测恢复）。
  db.prepare(`UPDATE tasks SET status = 'running', heartbeat_at = ? WHERE id = ?`).run(nowMs() - 10 * 60 * 1000, taskId);
  const { recoverStaleTasks } = await import('./durable.mjs');
  const recovered = recoverStaleTasks();
  ok(recovered.includes(taskId), 'recoverStaleTasks picks up stale running task');

  // 7) durable 任务执行（P0 修复回归测试）：schedule_task 排一个立即到期的任务 → tick.runDueTasks
  //    应真正执行它（跑 agentic loop）→ 经 send_message 送达 → 任务标 done。旧代码这里会失败
  //    （task 永远 pending、提醒永不送达、最终被标 dead）。
  process.env.WECOM_TARGET_ID = process.env.WECOM_TARGET_ID || 'ownerSelftest';
  const { callTool } = await import('./tools.mjs');
  // 主路径 mock：scheduled task 的 agent 先 send_message(提醒原文)，看到 tool 结果后给最终文本收尾。
  llm.setMockHandler((req) => {
    const hasToolResult = req.messages.some((m) => m.role === 'tool');
    if (!hasToolResult) {
      return { content: '', toolCalls: [{ id: 'sc1', type: 'function', function: { name: 'send_message', arguments: JSON.stringify({ content: '记得喝水' }) } }] };
    }
    return { content: '已提醒子淇喝水。', toolCalls: [] };
  });
  const schedRes = await callTool('schedule_task', { note: '记得喝水', delay_ms: -1000 }, { sessionId: `wecom:${process.env.WECOM_TARGET_ID}` });
  ok(schedRes.ok && schedRes.result.task_id > 0, 'schedule_task 建到期任务返回 task_id');
  const schedTaskId = schedRes.result.task_id;
  await tick(); // runDueTasks 应认领并执行该到期任务
  const taskRow = db.prepare(`SELECT status FROM tasks WHERE id=?`).get(schedTaskId);
  ok(taskRow && taskRow.status === 'done', `到期任务被 runDueTasks 真执行并标 done（旧代码会卡 pending→dead；实际=${taskRow && taskRow.status}）`);
  const deliveredPrimary = db.prepare(`SELECT COUNT(*) c FROM outbox WHERE content LIKE '%记得喝水%'`).get().c;
  ok(deliveredPrimary >= 1, '主路径：定时任务经 send_message 送达 outbox');

  // 8) 结构性兜底必达：另排一个任务，mock 只回文本不发消息 → 兜底投递 reply，保证提醒不丢。
  llm.setMockHandler(() => ({ content: '到点了，提醒你那件事。', toolCalls: [] }));
  const schedRes2 = await callTool('schedule_task', { note: '兜底测试', delay_ms: -2000 }, { sessionId: `wecom:${process.env.WECOM_TARGET_ID}` });
  const schedTaskId2 = schedRes2.result.task_id;
  await tick();
  const taskRow2 = db.prepare(`SELECT status FROM tasks WHERE id=?`).get(schedTaskId2);
  ok(taskRow2 && taskRow2.status === 'done', '兜底路径任务也标 done');
  const deliveredFallback = db.prepare(`SELECT COUNT(*) c FROM outbox WHERE content LIKE '%到点了，提醒你那件事%'`).get().c;
  ok(deliveredFallback >= 1, '兜底路径：agent 没发消息时，结构性兜底把 reply 投递出去（提醒必达）');

  // 9) 崩溃重跑不重复送达（"不掉线 + 不重复打扰"核心保证）：同一 scheduled task 跑两次（模拟崩在跑中后重跑），
  //    send_message 同内容经 dedup_hash(含 taskId) 只送达一次。
  llm.setMockHandler((req) => {
    const hasToolResult = req.messages.some((m) => m.role === 'tool');
    if (!hasToolResult) return { content: '', toolCalls: [{ id: 'cr1', type: 'function', function: { name: 'send_message', arguments: JSON.stringify({ content: '幂等提醒X' }) } }] };
    return { content: 'ok', toolCalls: [] };
  });
  const idemTask = (await callTool('schedule_task', { note: '幂等提醒X', delay_ms: -1000 }, { sessionId: `wecom:${process.env.WECOM_TARGET_ID}` })).result.task_id;
  await tick(); // 首跑
  db.prepare(`UPDATE tasks SET status='pending', heartbeat_at=NULL, next_run_at=? WHERE id=?`).run(nowMs() - 1000, idemTask); // 模拟崩溃后被 recover 退回 pending
  await tick(); // 重跑
  const idemCount = db.prepare(`SELECT COUNT(*) c FROM outbox WHERE content='幂等提醒X'`).get().c;
  ok(idemCount === 1, '崩溃重跑：同一任务同内容经 dedup 只送达一次（不重复打扰子淇）');

  // 10) 周期任务（恢复的核心机制）：seed 一个"刚到点"的 agentic recurring job → tick 应触发它+落标，
  //     派出的任务由下一拍 runDueTasks 执行送达。
  const { addJob } = await import('./recurring.mjs');
  const nowCst = new Date(nowMs() + 8 * 3600 * 1000);
  nowCst.setUTCMinutes(nowCst.getUTCMinutes() - 1); // 设成 1 分钟前，避开分钟翻转边界
  const fireHm = `${String(nowCst.getUTCHours()).padStart(2, '0')}:${String(nowCst.getUTCMinutes()).padStart(2, '0')}`;
  const recJobId = addJob(db, { name: '测试周期', fireHm, dow: null, kind: 'agentic', action: { message: '到点了做周期任务' } });
  llm.setMockHandler((req) => {
    const ht = req.messages.some((m) => m.role === 'tool');
    if (!ht) return { content: '', toolCalls: [{ id: 'rc', type: 'function', function: { name: 'send_message', arguments: JSON.stringify({ content: '周期播报X' }) } }] };
    return { content: 'ok', toolCalls: [] };
  });
  await tick(); // step②.6 触发 recurring job → 落标 + 派 agentic task
  ok(db.prepare(`SELECT last_fired_date FROM recurring_jobs WHERE id=?`).get(recJobId).last_fired_date, 'recurring job 触发后落标（当天不重触发）');
  await tick(); // 下一拍 runDueTasks 跑派出的任务
  ok(db.prepare(`SELECT COUNT(*) c FROM outbox WHERE content='周期播报X'`).get().c >= 1, 'recurring agentic 任务被执行并送达');

  // 11) 原则11 端到端：去掉 ESM 前置拦截后，模型路由 + record_checkin + loop 短路守红线。
  const { setPending, getPending } = await import('./esm.mjs');

  //  11a) 打卡回复 → 模型调 record_checkin（还故意带一句解读试图越线）→ loop 短路用中性回执收尾，解读被结构丢弃。
  setPending(db, { type: 'evening_anchor' });
  llm.setMockHandler((req) => {
    const hasTool = req.messages.some((m) => m.role === 'tool');
    if (!hasTool) return { content: '你今天压力不大，挺好的，继续保持', toolCalls: [{ id: 'ck', type: 'function', function: { name: 'record_checkin', arguments: JSON.stringify({ raw_text: '都挺好，没什么特别的' }) } }] };
    return { content: '（不该到这）', toolCalls: [] };
  });
  const replyCk = await handleIncoming({ sessionId: 'wecom:ownerSelftest', userInput: '都挺好，没什么特别的' });
  ok(/记下了/.test(replyCk) && !/压力不大|继续保持|挺好的/.test(replyCk), '打卡回复→record_checkin→固定中性回执，模型解读被结构丢弃（红线守住）');
  ok(db.prepare("SELECT COUNT(*) c FROM esm_raw WHERE raw_text='都挺好，没什么特别的'").get().c >= 1, '原话经 record_checkin 落 esm_raw（不可逆层）');

  //  11b) pending 在，但用户发的是命令 → 模型不调 record_checkin、正常处理命令 → 命令不被吞、不污染 ESM。
  setPending(db, { type: 'morning_anchor' });
  llm.setMockHandler(() => ({ content: '好，我帮你查今天日程', toolCalls: [] }));
  const replyCmd = await handleIncoming({ sessionId: 'wecom:ownerSelftest', userInput: '帮我查下今天日程' });
  ok(/日程/.test(replyCmd), 'pending 在但用户发命令→模型正常处理（不再被吞成打卡）');
  ok(db.prepare("SELECT COUNT(*) c FROM esm_raw WHERE raw_text='帮我查下今天日程'").get().c === 0, '命令没被误登记进 esm_raw（吞命令根因已除）');
  ok(db.prepare("SELECT COUNT(*) c FROM episodes WHERE content='帮我查下今天日程'").get().c >= 1, '命令原话落 episodes（交给了 agent，原话不丢）');

  //  11c) 成串消息装配：模型看装配文本（含时间脚手架），record_checkin 落 esm_raw 的必须是纯原话。
  setPending(db, { type: 'evening_anchor' });
  llm.setMockHandler((req) => {
    const hasTool = req.messages.some((m) => m.role === 'tool');
    if (!hasTool) return { content: '', toolCalls: [{ id: 'ck2', type: 'function', function: { name: 'record_checkin', arguments: JSON.stringify({ raw_text: '压力3吧' }) } }] };
    return { content: '（不该到这）', toolCalls: [] };
  });
  const assembled = '【2 条连发消息｜按到达顺序】\n[22:31:05 文字] 压力3吧\n[22:31:09 文字] 没喝酒没咖啡';
  const replyBatch = await handleIncoming({ sessionId: 'wecom:ownerSelftest', userInput: assembled, rawUserInput: '压力3吧\n没喝酒没咖啡' });
  ok(/记下了|？/.test(replyBatch) && !/连发消息/.test(replyBatch), '装配轮打卡→record_checkin 走通（中性回执/追问，不回显脚手架）');
  ok(db.prepare("SELECT COUNT(*) c FROM esm_raw WHERE raw_text=?").get('压力3吧\n没喝酒没咖啡').c === 1, '装配轮 esm_raw 落纯原话（rawUserInput），不含时间脚手架');
  ok(db.prepare("SELECT COUNT(*) c FROM esm_raw WHERE raw_text LIKE '%连发消息%'").get().c === 0, 'esm_raw 不可逆层零脚手架污染');
  ok(db.prepare("SELECT COUNT(*) c FROM episodes WHERE content=?").get(assembled).c >= 1, 'user episode 存装配文本（逐字近窗保留时间事实）');

  // 12) 调度管理工具（原则11#3 状态可见/可操作）：schedule_recurring 建 → list_schedules 列 → cancel_schedule 停。
  //     callTool 已在上面 import。get_weather 走真实高德 fetch，不在离线 selftest 测（由服务器冒烟 + weather --live 覆盖）。
  const srRes = await callTool('schedule_recurring', { name: '测试吃药提醒', time: '08:00', task: '提醒子淇吃药' }, {});
  ok(srRes.ok && srRes.result.recurring_id > 0, 'schedule_recurring 建周期任务返回 id');
  const recId = srRes.result.recurring_id;
  const srDup = await callTool('schedule_recurring', { name: '测试吃药提醒', time: '09:00', task: '改时间试试' }, {});
  ok(srDup.result.ok === false && srDup.result.recurring_id === recId, '同名 schedule_recurring 幂等（不重复建，提示先取消）');
  const lsRes = await callTool('list_schedules', {}, {});
  ok(lsRes.ok && lsRes.result.recurring.some((j) => j.id === recId && /08:00/.test(j.schedule)), 'list_schedules 列出新建周期任务（含时刻）');
  const csRes = await callTool('cancel_schedule', { type: 'recurring', id: recId }, {});
  ok(csRes.ok && csRes.result.cancelled === 'recurring', 'cancel_schedule 停掉周期任务');
  ok(!(await callTool('list_schedules', {}, {})).result.recurring.some((j) => j.id === recId), '取消后不再列出（enabled=0 被过滤）');

  // 一次性提醒的列出 + 取消：schedule_task 建未来任务 → list_schedules.onetime 含它 → cancel → 标 done(cancelled)。
  const stRes = await callTool('schedule_task', { note: '一次性测试提醒', delay_ms: 3600000 }, { sessionId: `wecom:${process.env.WECOM_TARGET_ID}` });
  const oneId = stRes.result.task_id;
  ok((await callTool('list_schedules', {}, {})).result.onetime.some((t) => t.id === oneId && /一次性测试提醒/.test(t.note)), 'list_schedules 列出待触发的一次性提醒');
  const csTask = await callTool('cancel_schedule', { type: 'task', id: oneId }, {});
  ok(csTask.ok && csTask.result.cancelled === 'task', 'cancel_schedule 取消一次性提醒');
  const oneRow = db.prepare('SELECT status, result FROM tasks WHERE id=?').get(oneId);
  ok(oneRow.status === 'done' && JSON.parse(oneRow.result).cancelled === true, '取消的一次性任务标 done(cancelled)，runDueTasks 不再跑它');
  ok((await callTool('cancel_schedule', { type: 'task', id: oneId }, {})).ok === false, '再次取消已 done 的任务被拒（状态守卫）');

  console.log(`\nPASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

// CLI 入口（契约：import.meta.url === pathToFileURL(process.argv[1]).href）。
if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  if (process.argv.includes('--selftest')) {
    selftest().catch((e) => {
      console.error('[main] selftest crashed: %s\n%s', e.message, e.stack);
      process.exit(1);
    });
  } else {
    main().catch((e) => {
      console.error('[main] startup crashed: %s\n%s', e.message, e.stack);
      process.exit(1);
    });
  }
}
