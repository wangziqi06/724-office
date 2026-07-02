// llm.mjs —— LLM 收口：纯 fetch 调 OpenAI 兼容 /chat/completions + 弹性重试 + 双 provider fallback。
//
// 致命纪律②：每次外部调用必带 timeout（AbortController + LLM_TIMEOUT_MS），绝不无限等待拖垮 worker tick。
// 致命纪律③：本模块绝不被放进 db 事务里调用（由 loop/worker 保证调用时机；这里只管"发请求"）。
// 零运行时依赖：不引任何 SDK/框架，纯 globalThis.fetch（Node18+ 内置）。
// 失败要响：错误对象保留 statusCode（供 callWithResilience 按 4xx/5xx 分流）和 message，不静默吞。

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const DIR = import.meta.dirname;

// ---- 环境加载（与 digital-twin 一致：.env 覆盖 process.env） ----
// 为什么自带 loader 而非依赖 db.mjs：llm 是被 loop 依赖的叶子模块，不应反向依赖业务库，
// 且 selftest 要能在不建 db 的情况下单独跑。
function loadEnv() {
  const env = { ...process.env };
  const p = join(DIR, '.env');
  if (existsSync(p)) {
    for (const line of readFileSync(p, 'utf8').split('\n')) {
      if (line.trim().startsWith('#')) continue;
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m) env[m[1]] = m[2].trim();
    }
  }
  return env;
}
const ENV = loadEnv();

// 集中常量（契约：阈值不散落）
export const LLM_TIMEOUT_MS = parseInt(ENV.LLM_TIMEOUT_MS || '30000', 10);
const LLM_BASE_URL = ENV.LLM_BASE_URL || 'https://api.deepseek.com/v1';
const LLM_API_KEY = ENV.LLM_API_KEY || '';
const LLM_MODEL = ENV.LLM_MODEL || 'deepseek-v4-pro'; // deepseek-chat 已并入 v4-flash 并将于 2026-07-24 退名；默认改指 pro
const LLM_FALLBACK_BASE_URL = ENV.LLM_FALLBACK_BASE_URL || 'https://api.moonshot.cn/v1';
const LLM_FALLBACK_API_KEY = ENV.LLM_FALLBACK_API_KEY || '';
const LLM_FALLBACK_MODEL = ENV.LLM_FALLBACK_MODEL || 'moonshot-v1-8k';

// MOCK 开关：显式 LLM_MOCK=1 或 无主 key 时离线（不联网），让 selftest/无网环境可跑。
export const USE_MOCK = ENV.LLM_MOCK === '1' || !LLM_API_KEY;

// 主/备 provider 配置（callWithResilience 在主连续失败后切到 fallback）
const PROVIDERS = {
  primary: { baseUrl: LLM_BASE_URL, apiKey: LLM_API_KEY, model: LLM_MODEL, name: 'primary' },
  fallback: {
    baseUrl: LLM_FALLBACK_BASE_URL,
    apiKey: LLM_FALLBACK_API_KEY,
    model: LLM_FALLBACK_MODEL,
    name: 'fallback',
  },
};

// ---- mock handler（selftest 注入） ----
// 默认 mock：返回固定无 tool_call 文本，保证 USE_MOCK 下循环能终止。
let mockHandler = (_req) => ({ content: '[mock] 收到，这是离线占位回复。', toolCalls: [] });

export function setMockHandler(fn) {
  if (typeof fn !== 'function') throw new Error('setMockHandler 需要一个函数');
  mockHandler = fn;
}

// ---- chatCompletion：纯 fetch 调一次 /chat/completions ----
// 返回 { content, toolCalls, raw, model, provider }。
// provider 参数内部用（callWithResilience 切 fallback 时传 'fallback'）；对外契约签名不暴露它，
// 但允许通过 model 覆盖；这里额外接受 _provider 以支持 fallback 路由（下划线=内部用）。
export async function chatCompletion({
  messages,
  tools = null,
  model = LLM_MODEL,
  temperature = 0.3,
  thinking = 'off', // 'off'（默认，关思考=快+稳+工具安全）| 'on'（开思考，仅用于无工具/自处理 reasoning 的单次调用）
  responseFormat = null,
  signal = null,
  _provider = 'primary',
}) {
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new Error('chatCompletion: messages 不能为空');
  }

  // MOCK 模式：不联网，走注入的 mockHandler，便于断言 tool_calls。
  if (USE_MOCK) {
    const r = mockHandler({ messages, tools }) || {};
    return {
      content: r.content ?? '',
      reasoning: r.reasoning ?? '',
      toolCalls: Array.isArray(r.toolCalls) ? r.toolCalls : [],
      raw: { mock: true, ...r },
      model: model || 'mock',
      provider: 'mock',
    };
  }

  const prov = PROVIDERS[_provider] || PROVIDERS.primary;
  if (!prov.apiKey) {
    // 没 key 又非 mock：明确报错而不是发空 Authorization 静默 401
    const e = new Error(`provider ${prov.name} 缺少 API key`);
    e.statusCode = 0;
    throw e;
  }

  const url = prov.baseUrl.replace(/\/$/, '') + '/chat/completions';
  // DeepSeek 思考模式（thinking_mode）：只对主 provider 且确为 DeepSeek 端点时才发 thinking 字段
  //   —— Kimi/其它 OpenAI 兼容端不认这个字段，发了会 400，故用 isDeepseek 守住。
  // v4-pro 默认 thinking=enabled；若不显式关，带工具的主循环第二轮会因缺 reasoning_content 报 400（实测 T6）。
  // 规则：关思考显式发 {type:'disabled'}；开思考发 {type:'enabled'} 且【不发 temperature】（官方思考模式不支持该参数）。
  const isPrimary = _provider === 'primary';
  const isDeepseek = /deepseek/i.test(prov.baseUrl || '');
  // 结构护栏（原则2：用结构让错误不可能）：开思考 + 带 tools = 多轮工具循环需逐轮回传 reasoning_content，
  // 当前 loop 未实现该回传（Phase 2 才做）。故只要带 tools 就强制关思考，让 T6 类 400 从结构上不可能发生。
  const hasTools = Array.isArray(tools) && tools.length > 0;
  if (isPrimary && isDeepseek && thinking === 'on' && hasTools) {
    console.warn('[llm] thinking=on 与 tools 同时出现 → 已强制关思考（避免工具循环缺 reasoning_content 报 400）；Phase 2 实现回传后再放开');
  }
  const wantThinking = isPrimary && isDeepseek && thinking === 'on' && !hasTools;
  const body = {
    model: isPrimary ? model || prov.model : prov.model,
    messages,
  };
  if (isPrimary && isDeepseek) body.thinking = { type: wantThinking ? 'enabled' : 'disabled' };
  if (!wantThinking) body.temperature = temperature; // 关思考/非 DeepSeek：正常发 temperature；开思考：不发
  if (tools && tools.length > 0) {
    body.tools = tools;
    body.tool_choice = 'auto';
  }
  if (responseFormat) body.response_format = responseFormat;

  // 致命纪律②：内部 AbortController 控 timeout；外部 signal 也能取消（两者任一触发即中止）。
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(new Error('timeout')), LLM_TIMEOUT_MS);
  const onExtAbort = () => ac.abort(signal?.reason || new Error('aborted'));
  if (signal) {
    if (signal.aborted) ac.abort(signal.reason);
    else signal.addEventListener('abort', onExtAbort, { once: true });
  }

  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${prov.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
  } catch (err) {
    // 网络层失败（含 abort/timeout）：归类为可重试（无 statusCode → callWithResilience 视作可退避）
    const e = new Error(`[${prov.name}] fetch 失败: ${err.message}`);
    e.statusCode = err.name === 'AbortError' ? 408 : undefined; // 408=超时，归入可重试
    e.cause = err;
    throw e;
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener?.('abort', onExtAbort);
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    const e = new Error(`[${prov.name}] HTTP ${resp.status}: ${text.slice(0, 300)}`);
    e.statusCode = resp.status; // 供 callWithResilience 按 429/5xx vs 4xx 分流
    throw e;
  }

  const data = await resp.json();
  const msg = data?.choices?.[0]?.message || {};
  return {
    content: typeof msg.content === 'string' ? msg.content : '',
    // 开思考时 DeepSeek 把思维链放这里（与 content 同级）；关思考时为空。捕获出来供将来"深想"能力回传用。
    reasoning: typeof msg.reasoning_content === 'string' ? msg.reasoning_content : '',
    toolCalls: Array.isArray(msg.tool_calls) ? msg.tool_calls : [],
    raw: data,
    model: data?.model || body.model,
    provider: prov.name,
  };
}

// ---- callWithResilience：指数退避 + 错误码分流 + DeepSeek↔Kimi fallback ----
// 退避：1→2→4s，每次叠加 30% jitter，封顶 retries=2 次（即最多 3 次尝试）。
// 错误码分流：err.statusCode 429/5xx/408 → 退避重试；其它 4xx → 直接抛（重了也没用）。
// fallback：把 _provider 在主连续失败后切 'fallback'（主备穷尽才放弃）；不做三态熔断。
// fn 约定：接收 { provider } 提示当前该用哪个 provider，返回 Promise。
// 配合 chatCompletion 用法：callWithResilience(({provider}) => chatCompletion({...args, _provider: provider}))
export async function callWithResilience(fn, { retries = 2, baseMs = 1000 } = {}) {
  if (typeof fn !== 'function') throw new Error('callWithResilience: fn 必须是函数');

  // 尝试顺序：先 primary 把 retries 次退避用完，仍失败且 fallback 有 key → 再给 fallback 试一轮。
  const haveFallback = !USE_MOCK && !!LLM_FALLBACK_API_KEY;
  const providers = haveFallback ? ['primary', 'fallback'] : ['primary'];

  let lastErr;
  for (const provider of providers) {
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        return await fn({ provider, attempt });
      } catch (err) {
        lastErr = err;
        const code = err?.statusCode;
        const retriable =
          code === undefined || code === 408 || code === 429 || (code >= 500 && code < 600);

        if (!retriable) {
          // 4xx（认证/参数错误等）：换 provider 也大概率同样错，但 401/403 可能是单 provider key 问题
          // → 鉴权类（401/403）允许切下一个 provider；其余 4xx 直接抛（不浪费退避）。
          if ((code === 401 || code === 403) && provider !== providers[providers.length - 1]) {
            console.warn('[llm] %s 鉴权失败(%s)，切换 provider', provider, code);
            break; // 跳出 attempt 循环，进入下一个 provider
          }
          console.warn('[llm] 不可重试错误(%s)，放弃: %s', code, err.message);
          throw err;
        }

        if (attempt < retries) {
          const backoff = Math.round(baseMs * 2 ** attempt * (1 + Math.random() * 0.3));
          console.warn(
            '[llm] %s 第%d次失败(%s)，%dms 后重试: %s',
            provider,
            attempt + 1,
            code ?? 'net',
            backoff,
            err.message,
          );
          await sleep(backoff);
        } else {
          console.warn(
            '[llm] %s 重试耗尽(%d次)，%s',
            provider,
            retries + 1,
            haveFallback && provider === 'primary' ? '切换到 fallback' : '无更多 provider',
          );
        }
      }
    }
  }
  // 所有 provider 都失败：抛最后一个错（保留证据，含 statusCode）
  throw lastErr || new Error('callWithResilience: 未知失败');
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ---- 自检：mock 路径 + 退避分流（不联网） ----
const IS_MAIN =
  typeof process.argv[1] === 'string' && import.meta.url === pathToFileURL(process.argv[1]).href;

if (process.argv.includes('--selftest') && IS_MAIN) {
  (async () => {
    let pass = 0,
      fail = 0;
    const ok = (c, m) => {
      console.log(`  ${c ? '✓' : '✗'} ${m}`);
      c ? pass++ : fail++;
    };
    console.log(`llm.mjs selftest (USE_MOCK=${USE_MOCK})\n`);

    // 1. mock handler 注入 → tool_calls 可断言
    setMockHandler((req) => {
      if (req.messages.some((m) => m.content?.includes('用工具'))) {
        return {
          content: '',
          toolCalls: [
            { id: 'call_1', type: 'function', function: { name: 'query_db', arguments: '{}' } },
          ],
        };
      }
      return { content: '最终回复', toolCalls: [] };
    });

    if (USE_MOCK) {
      const r1 = await chatCompletion({ messages: [{ role: 'user', content: '请用工具查一下' }] });
      ok(r1.toolCalls.length === 1 && r1.toolCalls[0].function.name === 'query_db', 'mock 返回 tool_call 可断言');
      ok(r1.provider === 'mock', 'mock 模式 provider=mock，不联网');

      const r2 = await chatCompletion({ messages: [{ role: 'user', content: '随便聊聊' }] });
      ok(r2.content === '最终回复' && r2.toolCalls.length === 0, 'mock 无 tool_call 路径');
    } else {
      ok(true, '(非 mock 环境，跳过 mock 断言)');
    }

    // 2. callWithResilience：4xx 不重试，直接抛
    let calls = 0;
    try {
      await callWithResilience(
        async () => {
          calls++;
          const e = new Error('bad request');
          e.statusCode = 400;
          throw e;
        },
        { retries: 2, baseMs: 1 },
      );
      ok(false, '4xx 应抛出');
    } catch (e) {
      ok(e.statusCode === 400 && calls === 1, '400 错误不重试（只调 1 次）');
    }

    // 3. callWithResilience：5xx 退避重试，第3次成功
    calls = 0;
    const r = await callWithResilience(
      async () => {
        calls++;
        if (calls < 3) {
          const e = new Error('server error');
          e.statusCode = 503;
          throw e;
        }
        return 'recovered';
      },
      { retries: 2, baseMs: 1 },
    );
    ok(r === 'recovered' && calls === 3, '503 退避重试，第3次成功');

    // 4. callWithResilience：网络错误（无 statusCode）也重试
    calls = 0;
    try {
      await callWithResilience(
        async () => {
          calls++;
          throw new Error('ECONNRESET'); // 无 statusCode
        },
        { retries: 2, baseMs: 1 },
      );
      ok(false, '应耗尽重试后抛');
    } catch {
      ok(calls === 3, '网络错误（无 code）按可重试处理，尝试 3 次');
    }

    console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
    process.exit(fail ? 1 : 0);
  })();
}
