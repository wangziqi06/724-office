// loop.mjs —— 手写 agentic 工具循环（while），小王的"思考-调工具-回灌-再思考"主循环。
//
// 致命纪律③：LLM 调用全在 db 事务外。本循环只编排"组上下文→chatCompletion→callTool→回灌"，
//   chatCompletion 经 callWithResilience 走纯 fetch（带 timeout），绝不在 tx() 里跑。
// 工具调用经 tools.callTool（致命纪律①的统一 wrapper），副作用去重在那里发生。
// 护栏（自愈原则）：maxTurns20 + wallclock180s + no-progress（相同 tool+params hash 连续 2 次）。
//   崩溃恢复靠 worker 整体重跑 + outbox dedup_hash，这里只防"单次循环跑飞/打转烧 token"。

import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import { nowMs } from './db.mjs';
import { chatCompletion, callWithResilience } from './llm.mjs';
import { TOOLS, toOpenAITools, callTool } from './tools.mjs';
import { buildSystemPrompt, buildMessages, fmtNowZh } from './prompt.mjs';
import { appendEpisode } from './memory.mjs';

// ---- 护栏常量（契约：阈值不散落，集中导出） ----
export const GUARDS = {
  maxTurns: 20, // 单次循环最多 20 轮 LLM 往返
  wallclockMs: 180000, // 墙钟 180s 上限（防卡死/慢 provider 拖垮）
  noProgressRepeats: 2, // 相同 (tool_name+params) 连续 2 次即判打转
};

const RENDERED_TOOLS = toOpenAITools(TOOLS);

// 工具调用指纹：tool_name + 规范化 params 的 hash，用于 no-progress 检测。
function toolCallFingerprint(name, argsRaw) {
  let normArgs = '';
  try {
    // 规范化：把 JSON 解析再排序键序列化，避免键顺序/空白造成"看似不同实则同"
    const obj = typeof argsRaw === 'string' ? JSON.parse(argsRaw || '{}') : argsRaw || {};
    normArgs = JSON.stringify(sortKeys(obj));
  } catch {
    normArgs = String(argsRaw ?? '');
  }
  return createHash('sha256').update(`${name}\0${normArgs}`).digest('hex').slice(0, 16);
}

function sortKeys(o) {
  if (Array.isArray(o)) return o.map(sortKeys);
  if (o && typeof o === 'object') {
    const out = {};
    for (const k of Object.keys(o).sort()) out[k] = sortKeys(o[k]);
    return out;
  }
  return o;
}

// ---- runAgentic：单次完整 agentic 循环 ----
// 返回 { reply, turns, stopReason }。stopReason ∈ 'done'|'max_turns'|'wallclock'|'no_progress'|'error'。
export async function runAgentic({
  taskId = null,
  sessionId = null,
  userInput,
  rawUserInput = null, // 主人的纯原话（成串装配时与 userInput 分离；null=二者相同；''=纯媒体轮无原话，让 record_checkin 空守卫生效）
  history = [],
  summary = '',
  anchors = [],
  recalled = [],
  recallWeak = false,
  pendingCheckin = null,
  sinceLastMs = null, // 距上一轮对话的间隔（assembleContext 算好传入）：进 system「现在」段，治轮间时间盲
}) {
  const startedAt = nowMs();

  // 上下文由 context.assembleContext 在上游组好（top-k 注入，绝不整份塞库——原则8），这里直接用、不再自查库。
  // system 段在循环【外】一次构建：单次 runAgentic 内 anchors/summary/recalled 冻结，
  // 移出 while 避免每轮重传同一块（3Mbps 上是真延迟），也让 system 前缀稳定 → provider context caching 可命中。
  const system = buildSystemPrompt({ anchors, summary, recalled, recallWeak, pendingCheckin, now: nowMs(), sinceLastMs });

  // working 上下文：从逐字近窗 history 起步，循环内 append 每轮的 assistant/tool 消息。
  // 注意 working 不落库（durable 状态在 tasks/steps/outbox），这里只是内存消息栈。
  const working = Array.isArray(history) ? [...history] : [];

  // 当前轮的用户输入只在第一轮注入（后续轮靠 working 里的 tool 结果驱动）。
  // 〔此刻…〕时间戳钉在当前消息上（仅 API 视图，不进 episodes/esm_raw）：实测 system 末尾的
  // 「# 现在」段会被近窗里模型自己说过的旧时间压过（错答一次即自我固化）——紧贴问题的时间戳
  // 是最高显著性位置，结构性防沿用（原则2：不指望模型自觉跨长距离对齐权威时间）。
  const nowStamp = `〔此刻 ${fmtNowZh(nowMs())}〕\n`;
  let pendingUserInput = userInput != null && userInput !== '' ? nowStamp + userInput : userInput;

  // ctx 带 userInput：record_checkin 这类工具要拿"用户这条真实原话"做不可逆登记（不信任模型复述）。
  // 成串消息时模型看的是带时间脚手架的装配文本（userInput），不可逆层只认纯原话（rawUserInput）。
  const ctx = { taskId, sessionId, userInput: rawUserInput ?? userInput };
  let lastFingerprint = null;
  let repeatCount = 0;
  let stopReason = 'done';
  let reply = '';
  let turns = 0;

  while (true) {
    // ---- 护栏 1：轮数 ----
    if (turns >= GUARDS.maxTurns) {
      stopReason = 'max_turns';
      console.warn('[loop] 触发 maxTurns(%d) 终止', GUARDS.maxTurns);
      break;
    }
    // ---- 护栏 2：墙钟 ----
    if (nowMs() - startedAt > GUARDS.wallclockMs) {
      stopReason = 'wallclock';
      console.warn('[loop] 触发 wallclock(%dms) 终止', GUARDS.wallclockMs);
      break;
    }

    // 组上下文：system 段已在循环外构建一次（本轮冻结），这里只拼 messages（working 每轮增长）。
    const messages = buildMessages({
      system,
      history: working,
      userInput: turns === 0 ? pendingUserInput : null,
    });
    // 第一轮的 userInput 一旦进了 messages，就并入 working，后续轮不再单独注入。
    if (turns === 0 && pendingUserInput != null && pendingUserInput !== '') {
      working.push({ role: 'user', content: String(pendingUserInput) });
      pendingUserInput = null;
    }

    // ---- LLM 调用（致命纪律③：tx 外；带 timeout + 弹性重试 + provider fallback） ----
    let resp;
    try {
      // 墙钟剩余时间做为本次调用的外层 signal，避免单次 LLM 超 wallclock。
      const remain = GUARDS.wallclockMs - (nowMs() - startedAt);
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(new Error('wallclock')), Math.max(1, remain));
      try {
        resp = await callWithResilience(({ provider }) =>
          chatCompletion({
            messages,
            tools: RENDERED_TOOLS,
            signal: ac.signal,
            _provider: provider,
          }),
        );
      } finally {
        clearTimeout(t);
      }
    } catch (err) {
      // 墙钟 signal 主动中止 LLM ≠ "出错"。区分开：被墙钟砍断走 wallclock 兜底文案，别对子淇说"内部出错"。
      const aborted = /wallclock|aborted/i.test(err.message || '');
      stopReason = aborted ? 'wallclock' : 'error';
      reply = aborted ? '' : `（小王内部出错：${err.message}）`;
      console.error('[loop] LLM 调用%s: %s', aborted ? '被墙钟中止' : '最终失败', err.message);
      break;
    }

    turns++;
    const toolCalls = resp.toolCalls || [];

    // ---- 无 tool_call：把 content 作为最终 reply 终止 ----
    if (toolCalls.length === 0) {
      reply = resp.content || '';
      stopReason = 'done';
      // 记 assistant 终态到 working（episodes 由 main.handleIncoming 负责落，循环内不重复落）
      working.push({ role: 'assistant', content: reply });
      break;
    }

    // ---- 有 tool_call：先把 assistant 的 tool_calls 消息入栈（OpenAI 协议要求） ----
    working.push({
      role: 'assistant',
      content: resp.content || null,
      tool_calls: toolCalls,
    });

    // no-progress 检测用第一个 tool_call 的指纹（多工具同轮则用整批拼一起）
    const batchFp = toolCalls
      .map((tc) => toolCallFingerprint(tc.function?.name, tc.function?.arguments))
      .join('|');
    if (batchFp === lastFingerprint) {
      repeatCount++;
      if (repeatCount >= GUARDS.noProgressRepeats) {
        stopReason = 'no_progress';
        console.warn('[loop] 触发 no-progress（相同工具调用重复 %d 次）终止', GUARDS.noProgressRepeats);
        // 给 LLM 一个收尾机会：回灌一条提示后不再循环
        reply = resp.content || '（小王在重复同一个动作，已停止避免空转。）';
        break;
      }
    } else {
      lastFingerprint = batchFp;
      repeatCount = 0;
    }

    // ---- 逐个执行 tool_call，结果以 role:'tool' 回灌 ----
    let terminalHit = false;
    for (const tc of toolCalls) {
      const name = tc.function?.name;
      let args = {};
      try {
        args = tc.function?.arguments ? JSON.parse(tc.function.arguments) : {};
      } catch (e) {
        // 参数 JSON 坏掉：回灌错误让 LLM 重试，不崩循环
        working.push({
          role: 'tool',
          tool_call_id: tc.id,
          content: JSON.stringify({ ok: false, error: `参数 JSON 解析失败: ${e.message}` }),
        });
        // 记到 episodes 供回溯
        safeEpisode({ sessionId, role: 'tool', content: `${name} 参数解析失败`, taskId });
        continue;
      }

      // 致命纪律①：经统一 wrapper callTool（内部算 dedup/过黑名单/包异常）
      const out = await callTool(name, args, ctx);
      const payload = out.ok
        ? { ok: true, deduped: out.deduped === true, result: out.result }
        : { ok: false, error: out.error };

      working.push({
        role: 'tool',
        tool_call_id: tc.id,
        content: JSON.stringify(payload).slice(0, 8192), // 截断防塞爆上下文
      });

      // 工具结果落 episodes（只追加，供后续召回）
      safeEpisode({
        sessionId,
        role: 'tool',
        content: `${name} → ${out.ok ? 'ok' : 'err:' + out.error}`,
        entity: name,
        taskId,
      });

      // 终止工具（如 record_checkin）：用它的固定中性回执作为最终 reply，丢弃模型本轮自由发挥，立即收尾。
      // 这是 ESM 红线"只问不评"的结构性最后一道闸——不寄望模型自觉不解读（原则2/3/11）。
      if (out.ok && out.result && out.result.terminal) {
        reply = String(out.result.reply ?? '');
        stopReason = 'done';
        terminalHit = true;
        break;
      }
    }
    if (terminalHit) break; // 终止工具触发 → 跳出 while，不再请求 LLM
    // 回到 while 顶，带着 tool 结果再请求一轮
  }

  // 护栏终止（轮数/墙钟/被砍）且没产出文本时，给子淇一个可读兜底，而不是发空消息或"内部出错"。
  // 这是"长任务处理得好"的体验保障：复杂任务超时被截断，也要有意义地交代，而非静默或报错。
  if ((!reply || !reply.trim()) && (stopReason === 'wallclock' || stopReason === 'max_turns' || stopReason === 'no_progress')) {
    reply = '这个事情有点复杂，我处理到一半先停下了。你要我接着弄哪一部分，或者拆成更小的步骤我再来？';
  }

  return { reply, turns, stopReason };
}

// episodes 落库失败不能拖垮循环（失败要响但降级）
function safeEpisode(args) {
  try {
    appendEpisode(args);
  } catch (e) {
    console.error('[loop] appendEpisode 失败(忽略): %s', e.message);
  }
}

// ---- 自检：mock LLM 驱动一次 tool_call→回灌→终止 + 护栏 ----
// 依赖真实 db/memory/tools，故需临时 db。参照 main.mjs 的 selftest 结构，但这里只测循环本身。
const IS_MAIN =
  typeof process.argv[1] === 'string' && import.meta.url === pathToFileURL(process.argv[1]).href;

if (process.argv.includes('--selftest') && IS_MAIN) {
  (async () => {
    const { mkdtempSync } = await import('node:fs');
    const { tmpdir } = await import('node:os');
    const { join } = await import('node:path');
    // 用临时 db（WAL 不支持 :memory:，故用临时文件）
    const tmp = mkdtempSync(join(tmpdir(), 'xw2loop-'));
    process.env.XW2_DB_PATH = join(tmp, 'v2.db');
    process.env.XW2_SANDBOX_DIR = join(tmp, 'ws');
    process.env.LLM_MOCK = '1'; // 强制离线
    process.env.ADAPTER_MOCK = '1';

    // 必须在设置 env 后才 import 这些模块（它们读 env 初始化）
    const { initDb } = await import('./db.mjs');
    const llm = await import('./llm.mjs');
    initDb();

    let pass = 0,
      fail = 0;
    const ok = (c, m) => {
      console.log(`  ${c ? '✓' : '✗'} ${m}`);
      c ? pass++ : fail++;
    };
    console.log('loop.mjs selftest (mock LLM + 临时 db)\n');

    // 脚本化 mock：第1轮发 memory_search tool_call，第2轮（看到 tool 结果后）给最终回复。
    let mockTurn = 0;
    let firstReqUserMsg = null; // 捕获第一轮请求里的当前 user 消息（验〔此刻…〕时间戳注入）
    llm.setMockHandler((req) => {
      const hasToolResult = req.messages.some((m) => m.role === 'tool');
      if (!hasToolResult) {
        mockTurn++;
        firstReqUserMsg = [...req.messages].reverse().find((m) => m.role === 'user')?.content ?? null;
        return {
          content: '',
          toolCalls: [
            {
              id: 'c1',
              type: 'function',
              function: { name: 'memory_search', arguments: JSON.stringify({ query: 'test' }) },
            },
          ],
        };
      }
      return { content: '查完了，这是最终回复。', toolCalls: [] };
    });

    const r1 = await runAgentic({ sessionId: 's1', userInput: '帮我查点东西' });
    ok(r1.stopReason === 'done', '正常路径 stopReason=done');
    ok(r1.reply === '查完了，这是最终回复。', '工具回灌后拿到最终 reply');
    ok(r1.turns === 2, '走了 2 轮（1 工具 + 1 终态）');
    ok(
      typeof firstReqUserMsg === 'string' && /^〔此刻 \d{4}-\d{2}-\d{2} \d{2}:\d{2}（周[日一二三四五六]）〕\n帮我查点东西$/.test(firstReqUserMsg),
      '当前 user 消息带〔此刻…〕时间戳（仅 API 视图，防模型沿用历史旧时间）',
    );

    // no-progress：mock 永远发同一个 tool_call → 应在重复 noProgressRepeats 次后停。
    llm.setMockHandler(() => ({
      content: '',
      toolCalls: [
        {
          id: 'cX',
          type: 'function',
          function: { name: 'memory_search', arguments: JSON.stringify({ query: 'loop' }) },
        },
      ],
    }));
    const r2 = await runAgentic({ sessionId: 's2', userInput: '空转测试' });
    ok(r2.stopReason === 'no_progress', '相同工具重复触发 no_progress 终止');
    ok(r2.turns <= GUARDS.maxTurns, 'no_progress 在 maxTurns 之前就停');

    // max_turns：mock 每轮发"不同 args"的 tool_call（避开 no-progress），应撞 maxTurns。
    let n = 0;
    llm.setMockHandler(() => ({
      content: '',
      toolCalls: [
        {
          id: 'cm' + n,
          type: 'function',
          function: { name: 'memory_search', arguments: JSON.stringify({ query: 'q' + n++ }) },
        },
      ],
    }));
    const r3 = await runAgentic({ sessionId: 's3', userInput: '撞墙测试' });
    ok(r3.stopReason === 'max_turns' && r3.turns === GUARDS.maxTurns, 'maxTurns 护栏生效');
    ok(r3.reply && r3.reply.trim().length > 0, 'maxTurns 终止也给可读兜底文案（不发空消息）');

    // 坏参数 JSON：回灌错误不崩；mock 第二轮收尾。
    let badTurn = 0;
    llm.setMockHandler((req) => {
      const hasTool = req.messages.some((m) => m.role === 'tool');
      if (!hasTool) {
        badTurn++;
        return {
          content: '',
          toolCalls: [{ id: 'cb', type: 'function', function: { name: 'memory_search', arguments: '{bad json' } }],
        };
      }
      return { content: '已处理坏参数', toolCalls: [] };
    });
    const r4 = await runAgentic({ sessionId: 's4', userInput: '坏参数' });
    ok(r4.stopReason === 'done', '坏参数 JSON 回灌错误后循环不崩，正常收尾');

    console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
    process.exit(fail ? 1 : 0);
  })();
}
