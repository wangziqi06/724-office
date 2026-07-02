// tools.mjs —— 工具注册表 + 统一执行入口 callTool（致命纪律①的落地处）。
//
// 致命纪律①：每个有副作用的工具调用前，必须经统一 wrapper（callTool）算 dedup_hash 并查/写
//   outbox（或幂等键）去重，命中即不重复执行。这条在 callTool 里强制实现，工具 fn 自身不碰去重。
// 致命纪律②：http_get 的 fetch 必带 timeout（HTTP_TIMEOUT_MS）。
// 致命纪律③：fn 内的 fetch/HTTP 在事务外；最终落库（enqueueOutbox/stageFact/createTask）才进 tx()，
//   且那些 tx() 都在被依赖模块内部，本模块只调它们的同步接口、不在 tx 里 await。
//
// 安全分层（团队原则3）：
//   - Schema 层：query_db 只读、参数化，不暴露任意 SQL（工具定义里就没有"任意 SQL"入口）。
//   - 工具校验层：resolveSandboxPath 防路径穿越；DANGEROUS_PATTERNS 拦非 SELECT / SSRF 内网地址。
//   - 审批层：memory_write 进 staging 等人工确认，不直接写 facts 表。

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync, openSync, fsyncSync, closeSync, renameSync, statSync } from 'node:fs';
import { join, resolve, sep, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

import { getDb, nowMs } from './db.mjs';
import { createTask, scheduleTimer, markTaskDone } from './durable.mjs';
import { appendEpisode, retrieve, stageFact, pinFact, unpinFact, pinnedFacts } from './memory.mjs';
import { enqueueOutbox } from './adapter.mjs';
import { recordCheckin } from './esm.mjs';
import { weatherBriefing, ADCODE } from './weather.mjs';
import { initRecurringSchema, addJob, listJobs, setEnabled } from './recurring.mjs';
import { IDENTITY } from './identity.mjs';

const DIR = import.meta.dirname;

// 沙箱根：read_file/write_file 只能在此目录内操作（path traversal 防护）
export const SANDBOX_DIR = resolve(process.env.XW2_SANDBOX_DIR || join(DIR, 'workspace'));

const HTTP_TIMEOUT_MS = parseInt(process.env.HTTP_TIMEOUT_MS || '15000', 10);

// ---- dedup_hash（契约权威算法：sha256 hex 前16，规范化后空格拼接） ----
// 必须与 durable.mjs / adapter.mjs / main.mjs 的 dedupHash 完全一致（同一逻辑消息跨路径
// 算出同一 hash 才能真正去重）。契约规定的分隔符是空格（join(' ')），这里对齐它——
// 之前用 '\0' 会与其它模块算出不同 hash，导致跨路径去重静默失效。
function dedupHash(parts) {
  const norm = parts.map((p) => String(p ?? '').trim()).join(' ');
  return createHash('sha256').update(norm).digest('hex').slice(0, 16);
}

// ---- 危险操作黑名单（callTool 对 dangerous 工具过这些正则） ----
// query_db：拦一切非 SELECT 开头的语句（写操作不该走只读工具）。
const SQL_WRITE_RE = /^\s*(insert|update|delete|drop|alter|attach|detach|pragma|create|replace|vacuum|reindex)/i;
// http_get：拦内网/本地/云元数据地址防 SSRF。覆盖点分私网、链路本地、阿里云元数据(100.100.100.x)、
// IPv6 回环/ULA/链路本地，以及十六/八/十进制 IP 编码绕过（纯正则、不做 DNS 解析——按审计共识不上重型 SSRF 改造）。
const SSRF_PATTERNS = [
  /^https?:\/\/(localhost|127\.|0\.0\.0\.0|169\.254\.|100\.100\.100\.)/i,
  /^https?:\/\/10\./i,
  /^https?:\/\/192\.168\./i,
  /^https?:\/\/172\.(1[6-9]|2\d|3[01])\./i,
  /^https?:\/\/\[(::1|::|::ffff:|f[cd]|fe[89ab])/i,   // IPv6 回环/未指定/IPv4映射/ULA/链路本地
  /^https?:\/\/0x[0-9a-f]+([/:?#]|$)/i,               // 十六进制 IP (0x7f000001)
  /^https?:\/\/0[0-7]+(\.|[/:?#]|$)/i,                // 八进制 IP (0177.0.0.1)
  /^https?:\/\/\d{8,10}([/:?#]|$)/,                   // 纯十进制整数 IP (2130706433)
];

// ---- 沙箱路径解析（read_file/write_file 必经） ----
// 为什么断言 startsWith(SANDBOX_DIR + sep)：单纯 startsWith(SANDBOX_DIR) 会让
// /workspace-evil 通过；加分隔符确保是"目录内"而非"前缀相同"。
export function resolveSandboxPath(p) {
  if (typeof p !== 'string' || p.length === 0) throw new Error('path 不能为空');
  const resolved = resolve(SANDBOX_DIR, p);
  if (resolved !== SANDBOX_DIR && !resolved.startsWith(SANDBOX_DIR + sep)) {
    throw new Error('sandbox escape blocked: ' + p);
  }
  return resolved;
}

// 原子写：临时文件 → fsync → rename（自愈原则的文件产物保护，避免半截文件）
function atomicWrite(absPath, content) {
  const tmp = absPath + '.tmp';
  writeFileSync(tmp, content, 'utf8');
  const fd = openSync(tmp, 'r+');
  try {
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, absPath);
}

// =====================================================================
// 8 个工具的实现
// =====================================================================

// 1. memory_search —— 召回历史记忆（只读，无副作用）
async function tool_memory_search(args) {
  const query = String(args.query ?? '');
  const k = clampInt(args.k, 8, 1, 30);
  const hits = retrieve(query, k);
  return { count: hits.length, results: hits };
}

// 2. memory_write —— 写记忆（有副作用：进 facts_staging 等人工确认，不直接入 facts）
async function tool_memory_write(args) {
  const fact = String(args.fact ?? '').trim();
  if (!fact) throw new Error('fact 不能为空');
  const id = stageFact({
    entity: args.entity ? String(args.entity) : null,
    fact,
    source: ['user_said', 'inferred', 'external'].includes(args.source) ? args.source : 'inferred',
  });
  return { staged_id: id, status: 'pending', note: '已进 staging，等人工确认后才入正式 facts' };
}

// 2b. pin_fact —— 子淇【显式】要记住某事时，直接钉成长期锚点（pinned=1，立即注入每轮上下文）。
// 与 memory_write 的边界（原则11，crisp 互不重叠）：
//   memory_write = 模型自己推断的事实 → 进 staging 等人工确认（半自动护栏）。
//   pin_fact     = 子淇明说"记住/钉住" → 直达 pinned 锚点，不经 staging。
// 这两件事此前被挤在一个 memory_write 里，导致"说记住"卡死在 staging（裂缝二）；拆成两个工具。
async function tool_pin_fact(args) {
  const fact = String(args.fact ?? '').trim();
  if (!fact) throw new Error('fact 不能为空');
  const { id, deduped } = pinFact({ entity: args.entity ? String(args.entity) : null, fact });
  return {
    fact_id: id,
    pinned: true,
    deduped,
    note: deduped ? '已钉过(去重)' : '已钉成长期锚点，以后每次对话都会记着',
  };
}

// 2c. unpin_fact —— 取消一条记错/过时的锚点（pin_fact 的逆操作，闭合本体"会忘但能纠正"）。
// 精确匹配不到时把当前锚点列出来让模型对准原话再试（原则11#3：要取消 X 先能看见 X，不靠猜 id）。
async function tool_unpin_fact(args) {
  const fact = String(args.fact ?? '').trim();
  if (!fact) throw new Error('fact 不能为空');
  const { unpinned } = unpinFact(fact);
  if (unpinned) return { unpinned: true, note: '已取消这条锚点，以后不再记着了' };
  const current = pinnedFacts(20).map((f) => f.fact);
  return {
    unpinned: false,
    note: '没找到完全匹配的锚点，没动任何东西。当前钉着的是这些，请用其中的原话再试：',
    current_pins: current,
  };
}

// 3. read_file —— 读沙箱内文件（只读）
async function tool_read_file(args) {
  const abs = resolveSandboxPath(args.path);
  if (!existsSync(abs)) throw new Error('文件不存在: ' + args.path);
  const st = statSync(abs);
  if (st.isDirectory()) throw new Error('是目录不是文件: ' + args.path);
  // 上限 256KB，防一次塞爆上下文（原则8）
  if (st.size > 256 * 1024) throw new Error(`文件过大(${st.size}B)，超过 256KB 上限`);
  return { path: args.path, content: readFileSync(abs, 'utf8') };
}

// 4. write_file —— 写沙箱内文件（有副作用：原子写）
async function tool_write_file(args) {
  const abs = resolveSandboxPath(args.path);
  const content = String(args.content ?? '');
  // 确保父目录在沙箱内（resolveSandboxPath 已校验整路径，这里只防父目录不存在）
  const parent = dirname(abs);
  if (!existsSync(parent)) throw new Error('父目录不存在: ' + args.path);
  atomicWrite(abs, content);
  return { path: args.path, bytes: Buffer.byteLength(content, 'utf8') };
}

// 5. http_get —— 外部 GET（有 timeout，无副作用但拦 SSRF）
async function tool_http_get(args) {
  const url = String(args.url ?? '');
  if (!/^https?:\/\//i.test(url)) throw new Error('仅支持 http/https URL');
  // 致命纪律②：AbortController + HTTP_TIMEOUT_MS
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(new Error('timeout')), HTTP_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { method: 'GET', signal: ac.signal, redirect: 'follow' });
    // 防重定向 SSRF（零回归纵深防护）：跟随跳转后，若【最终落点】落在内网/元数据网段，
    // 丢弃响应体不回灌给 agent——正常外网跳转 resp.url 仍是外网、照常返回，合法重定向不受影响。
    const finalUrl = resp.url || url;
    if (SSRF_PATTERNS.some((re) => re.test(finalUrl))) {
      throw new Error(`重定向落点被拦截(SSRF 防护): ${finalUrl}`);
    }
    const text = await resp.text();
    return {
      status: resp.status,
      ok: resp.ok,
      // 截断防塞爆上下文
      body: text.slice(0, 8192),
      truncated: text.length > 8192,
    };
  } catch (err) {
    throw new Error(`http_get 失败: ${err.message}`);
  } finally {
    clearTimeout(timer);
  }
}

// 6. schedule_task —— 排定时任务/提醒（有副作用：写 tasks/timers）
async function tool_schedule_task(args, ctx) {
  const delayMs = Number(args.delay_ms);
  const fireAt = Number.isFinite(delayMs) ? nowMs() + delayMs : Number(args.fire_at);
  if (!Number.isFinite(fireAt)) throw new Error('需要 delay_ms 或 fire_at(epoch ms)');
  const payload = {
    note: String(args.note ?? ''),
    target: ctx?.sessionId ?? null,
    origin_task: ctx?.taskId ?? null,
  };
  // 排一个 agentic 任务承载提醒逻辑，并落 timer 到点唤醒它。
  // 幂等键按 conventions：dedupHash([kind, payload, 业务键(fireAt)])
  const idem = dedupHash(['agentic', JSON.stringify(payload), String(fireAt)]);
  const { taskId, deduped } = createTask({
    kind: 'agentic',
    payload,
    idempotencyKey: idem,
    nextRunAt: fireAt,
  });
  const timerId = scheduleTimer({ fireAt, taskId, payload, catchupPolicy: 'once' });
  return { task_id: taskId, timer_id: timerId, fire_at: fireAt, deduped };
}

// 7. send_message —— 给 owner 发消息（有副作用：走 outbox，绝不直发）
async function tool_send_message(args, ctx) {
  const content = String(args.content ?? '').trim();
  if (!content) throw new Error('content 不能为空');
  const channel = args.channel || 'wecom';
  // 安全·Schema 层（团队原则3）：收件人【恒为 owner】，忽略 LLM 给的 target。
  // n=1 只发子淇自己，杜绝二阶 prompt injection 借小王身份给第三方发消息/外泄；模型也无法误填 target 发错人。
  const target = String(
    process.env.WECOM_TARGET_ID ||
    process.env.OWNER_ID ||
    (ctx?.sessionId ? String(ctx.sessionId).replace(/^wecom:/, '') : '') ||
    ''
  );
  if (!target) throw new Error('无收件人(owner 未配置 WECOM_TARGET_ID/OWNER_ID)');
  // dedup_hash 按 conventions：[channel, target, content, taskId ?? '']
  const hash = dedupHash([channel, target, content, ctx?.taskId ?? '']);
  const { id, deduped } = enqueueOutbox({ channel, target, content, dedupHash: hash });
  return { outbox_id: id, deduped, status: deduped ? '已派过(去重)' : 'queued' };
}

// 9. record_checkin —— 登记子淇对晨/晚打卡的回复（终止工具：确定性登记 + 固定中性回执，结构守红线）
async function tool_record_checkin(args, ctx) {
  // 原话以 harness 持有的真实输入为准（ctx.userInput），不信任模型对原话的复述（防改写污染不可逆层）。
  const raw = String(ctx?.userInput ?? args?.raw_text ?? '').trim();
  const out = await recordCheckin(getDb(), raw);
  if (!out.ok) throw new Error(out.error);
  return { reply: out.reply, terminal: true }; // terminal:true → loop 用 reply 收尾并丢弃模型自由发挥
}

// 8. query_db —— 只读查库（参数化，黑名单已在 callTool 拦非 SELECT）
async function tool_query_db(args) {
  const sql = String(args.sql ?? '');
  const params = Array.isArray(args.params) ? args.params : [];
  // 双保险：这里再校验一次（callTool 的 dangerous 黑名单是第一道）
  if (SQL_WRITE_RE.test(sql)) throw new Error('query_db 只读，拒绝非 SELECT 语句');
  if (/;\s*\S/.test(sql.trim().replace(/;\s*$/, ''))) throw new Error('不允许多语句(分号拼接)');
  const db = getDb();
  const stmt = db.prepare(sql);
  // 参数化：占位符 ? 由 params 填充，绝不字符串拼接
  const rows = stmt.all(...params);
  // 上限 200 行防塞爆上下文
  return { rows: rows.slice(0, 200), truncated: rows.length > 200, total: rows.length };
}

// 10. get_weather —— 按需查天气（确定性，复用 weather.mjs 高德链路）。
// 为什么要它：子淇随口问"明天天气"时，agent 之前只能用 http_get 去抓天气站（常被挡/超时，
// 线上真出现过"几个气候站都被挡了"），或凭记忆瞎说。这里直接走已验证的高德链路给准数据。
// 边界：这是【按需问答】；每天定点的天气播报是 recurring builtin（不经 agent、无 AI 味），别拿它"设每天播报"。
async function tool_get_weather(args) {
  const defaultCity = (IDENTITY.weatherMorningCities && IDENTITY.weatherMorningCities[0]) || '上海';
  const city = String(args.city ?? defaultCity).trim() || defaultCity;
  if (!ADCODE[city]) throw new Error(`暂只支持 ${Object.keys(ADCODE).join('/')}；「${city}」没有。`);
  const days = clampInt(args.days, 4, 1, 4); // 高德 forecast = 今 + 未来 3 天，最多 4
  const text = await weatherBriefing({ cities: [city], fromIdx: 0, days });
  return { city, days, weather: text };
}

// 11. schedule_recurring —— 排一个【每天/每周固定时刻】重复做的事（复用 recurring.mjs，agentic kind）。
// 与 schedule_task 的边界：schedule_task=一次性（几分钟后/某绝对时刻触发一次）；本工具=重复。
// 到点 worker 会把 task 当一件事派给 agent 去做（可调别的工具，如 get_weather）。
async function tool_schedule_recurring(args) {
  const db = getDb();
  initRecurringSchema(db); // 防御性幂等（正常 main 启动已建表）
  const name = String(args.name ?? '').trim();
  const time = String(args.time ?? '').trim();
  const task = String(args.task ?? '').trim();
  if (!name) throw new Error('name（给周期任务起个短名，如"吃药提醒"）不能为空');
  if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(time)) throw new Error("time 需 24 小时制 'HH:MM'，如 08:30");
  if (!task) throw new Error('task（到点要做/提醒什么）不能为空');
  // weekday 可选：0=周日…6=周六，省略=每天
  let dow = null;
  if (args.weekday != null && String(args.weekday).trim() !== '') {
    const n = Number.parseInt(args.weekday, 10);
    if (!Number.isInteger(n) || n < 0 || n > 6) throw new Error('weekday 需 0-6(0=周日)，省略=每天');
    dow = n;
  }
  // 同名幂等护栏：已存在不重复建（防重试/口误双发）。要改→先 cancel_schedule 再建。
  const existing = listJobs(db).find((j) => j.name === name);
  if (existing) {
    return { ok: false, recurring_id: existing.id, note: `已有同名周期任务「${name}」(id=${existing.id})。要改时间/内容，先 cancel_schedule 取消它再重建。` };
  }
  const id = addJob(db, { name, fireHm: time, dow, kind: 'agentic', action: { message: task } });
  const when = dow == null ? '每天' : `每周${'日一二三四五六'[dow]}`;
  return { recurring_id: id, name, schedule: `${when} ${time}`, task, note: '已排定，到点我会自动去做' };
}

// 12. list_schedules —— 列出当前所有定时安排（周期 + 一次性待触发）。只读。
// 原则11#3「要取消/改 X，先得能列出 X」：给模型真实 id，cancel_schedule 才有据可依（不靠猜）。
async function tool_list_schedules() {
  const db = getDb();
  initRecurringSchema(db);
  const recurring = listJobs(db)
    .filter((j) => j.enabled)
    .map((j) => ({ id: j.id, name: j.name, schedule: `${j.dow == null ? '每天' : '每周' + '日一二三四五六'[j.dow]} ${j.fire_hm}` }));
  const onetime = db
    .prepare(`SELECT id, next_run_at, payload FROM tasks WHERE status='pending' AND next_run_at IS NOT NULL ORDER BY next_run_at ASC LIMIT 30`)
    .all()
    .map((t) => {
      let note = '';
      try { note = (JSON.parse(t.payload) || {}).note || ''; } catch { /* payload 坏不拖垮列举 */ }
      return { id: t.id, fire_at: t.next_run_at, note };
    });
  return { recurring, onetime, empty: recurring.length === 0 && onetime.length === 0 };
}

// 13. cancel_schedule —— 取消一个周期任务或一次性提醒（按 list_schedules 给的 type+id）。
// 周期任务：禁用(enabled=0，可逆、不删行——保留台账便于将来恢复/对账)。
// 一次性：pending 任务标 done(cancelled)，runDueTasks 不再跑它；其绑定 timer 因 task_id 在只空转记录、不发送。
async function tool_cancel_schedule(args) {
  const db = getDb();
  initRecurringSchema(db);
  const type = String(args.type ?? '').trim();
  const id = Number.parseInt(args.id, 10);
  if (!Number.isInteger(id) || id < 1) throw new Error('id 无效（用 list_schedules 给出的数字 id）');
  if (type === 'recurring') {
    const job = listJobs(db).find((j) => j.id === id);
    if (!job) throw new Error(`没有 id=${id} 的周期任务（先 list_schedules 看看）`);
    setEnabled(db, id, 0);
    return { cancelled: 'recurring', id, name: job.name, note: `已停掉「${job.name}」` };
  }
  if (type === 'task') {
    const t = db.prepare(`SELECT id, status, payload FROM tasks WHERE id=?`).get(id);
    if (!t) throw new Error(`没有 id=${id} 的一次性提醒`);
    if (t.status !== 'pending') throw new Error(`这条提醒状态是 ${t.status}，不是待触发，取消不了`);
    markTaskDone(id, { cancelled: true, reason: '用户取消' });
    let note = ''; try { note = (JSON.parse(t.payload) || {}).note || ''; } catch { /* ignore */ }
    return { cancelled: 'task', id, note: note || '(一次性提醒)' };
  }
  throw new Error("type 需 'recurring'（周期任务）或 'task'（一次性提醒）");
}

// =====================================================================
// 注册表
// =====================================================================
export const TOOLS = [
  {
    name: 'memory_search',
    description: '检索小王的历史记忆/对话，返回最相关的 top-k 条。用于回忆之前聊过什么、做过什么。',
    paramSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '检索关键词或自然语言描述' },
        k: { type: 'integer', description: '返回条数，默认 8，最多 30' },
      },
      required: ['query'],
    },
    fn: tool_memory_search,
    sideEffect: false,
  },
  {
    name: 'memory_write',
    description: `把你从对话里【推断/观察】到的、关于${IDENTITY.ownerName}的事实写入暂存区（staging），等人工确认后才成为正式事实。用于你觉得值得记、但他没明说要记的事。他【明确要你记住/钉住】某事时改用 pin_fact（直接生效、不进暂存）。不要写未经证实的猜测。`,
    paramSchema: {
      type: 'object',
      properties: {
        fact: { type: 'string', description: '要记住的事实，一句话' },
        entity: { type: 'string', description: '事实所属主题/实体，可选' },
        source: { type: 'string', enum: ['user_said', 'inferred', 'external'], description: '来源' },
      },
      required: ['fact'],
    },
    fn: tool_memory_write,
    sideEffect: true,
  },
  {
    name: 'pin_fact',
    description: `当${IDENTITY.ownerName}【明确要你记住/钉住】某事时调用（他说"记住…""记一下…""以后都…"这类显式指令），直接把它钉成长期锚点——立刻生效、之后每次对话都带着。只在他显式要求记住时用；你自己从对话里觉得重要、但他没明说要记的，用 memory_write（进暂存区等确认）。`,
    paramSchema: {
      type: 'object',
      properties: {
        fact: { type: 'string', description: '要长期记住的事，一句话（尽量含具体信息，别只写"那件事"）' },
        entity: { type: 'string', description: '所属主题/实体，可选（如"居住""偏好""工作"）' },
      },
      required: ['fact'],
    },
    fn: tool_pin_fact,
    sideEffect: true,
  },
  {
    name: 'unpin_fact',
    description: `当${IDENTITY.ownerName}要你【取消/纠正/撤回】之前钉住的某条长期记忆时调用（他说"别记X了""那条记错了""忘掉X""我不再…了"）。传入要取消那条锚点的原话（尽量用你看到的锚点原文）。这是 pin_fact 的逆操作；若没完全匹配上，工具会把当前钉着的锚点列给你，用准确原话再试。`,
    paramSchema: {
      type: 'object',
      properties: {
        fact: { type: 'string', description: '要取消的那条锚点的原话（与当前钉着的某条尽量一致）' },
      },
      required: ['fact'],
    },
    fn: tool_unpin_fact,
    sideEffect: true,
  },
  {
    name: 'read_file',
    description: '读取沙箱工作目录内的文本文件。路径相对于沙箱根，禁止越界。',
    paramSchema: {
      type: 'object',
      properties: { path: { type: 'string', description: '沙箱内相对路径' } },
      required: ['path'],
    },
    fn: tool_read_file,
    sideEffect: false,
  },
  {
    name: 'write_file',
    description: '写入沙箱工作目录内的文本文件（原子写）。路径相对于沙箱根，禁止越界。',
    paramSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '沙箱内相对路径' },
        content: { type: 'string', description: '文件内容' },
      },
      required: ['path', 'content'],
    },
    fn: tool_write_file,
    sideEffect: true,
  },
  {
    name: 'http_get',
    description: '对外部 URL 发 GET 请求并返回响应体（截断）。仅 http/https，拦内网地址防 SSRF，带超时。',
    paramSchema: {
      type: 'object',
      properties: { url: { type: 'string', description: '完整 http/https URL' } },
      required: ['url'],
    },
    fn: tool_http_get,
    sideEffect: false,
    dangerous: true, // 过 SSRF 黑名单
  },
  {
    name: 'schedule_task',
    description: '排一个未来触发的任务/提醒。delay_ms=多少毫秒后触发（如 3 天后=259200000），或 fire_at=绝对 epoch ms。',
    paramSchema: {
      type: 'object',
      properties: {
        note: { type: 'string', description: '提醒内容/任务说明' },
        delay_ms: { type: 'integer', description: '多少毫秒后触发' },
        fire_at: { type: 'integer', description: '绝对触发时间(epoch ms)，与 delay_ms 二选一' },
      },
      required: ['note'],
    },
    fn: tool_schedule_task,
    sideEffect: true,
  },
  {
    name: 'send_message',
    description: `给${IDENTITY.ownerName}发一条消息（走 outbox，至少一次投递+尽力去重）。需要主动告知/提醒${IDENTITY.ownerName}时用，不要把内容只写进回复正文。固定发给${IDENTITY.ownerName}本人，无需也不能指定收件人。`,
    paramSchema: {
      type: 'object',
      properties: {
        content: { type: 'string', description: '消息正文' },
      },
      required: ['content'],
    },
    fn: tool_send_message,
    sideEffect: true,
  },
  {
    name: 'query_db',
    description: '只读查询小王的数据库（SELECT，参数化）。可查 tasks/timers/outbox/episodes/facts 等表。禁止写操作和多语句。',
    paramSchema: {
      type: 'object',
      properties: {
        sql: { type: 'string', description: '单条 SELECT 语句，用 ? 占位符' },
        params: { type: 'array', description: '占位符参数数组', items: {} },
      },
      required: ['sql'],
    },
    fn: tool_query_db,
    sideEffect: false,
    dangerous: true, // 过非 SELECT 黑名单
  },
  {
    name: 'record_checkin',
    description: `当${IDENTITY.ownerName}在回答你刚发的晨/晚自检打卡时调用，把他这条原话登记进采集库。只在他确实是在回打卡时调（说睡眠/精力/压力/情绪/喝酒咖啡运动这类）。这是纯登记动作，登记完只会回一句中性确认——你【绝不要】在调用前后对内容做任何评价/解读/打分/安慰/建议（采集红线）。他要不是在回打卡（是别的请求/闲聊/指令），就别调这个工具，正常帮他。`,
    paramSchema: {
      type: 'object',
      properties: {
        raw_text: { type: 'string', description: `${IDENTITY.ownerName}这条打卡回复的原话（系统会以实际收到的消息为准，你照填即可）` },
      },
      required: [],
    },
    fn: tool_record_checkin,
    sideEffect: true,
  },
  {
    name: 'get_weather',
    description: `查某城市的天气预报（今天+未来几天，确定性数据源）。${IDENTITY.ownerName}随口问天气时用。只支持上海/深圳/北京。注意：这是按需查询，不是"设置每天天气播报"（每天定点的天气播报已默认开着；要新增定点播报用 schedule_recurring）。`,
    paramSchema: {
      type: 'object',
      properties: {
        city: { type: 'string', enum: ['上海', '深圳', '北京'], description: '城市，默认上海' },
        days: { type: 'integer', description: '查几天(1-4，含今天)，默认 4' },
      },
      required: [],
    },
    fn: tool_get_weather,
    sideEffect: false,
  },
  {
    name: 'schedule_recurring',
    description: '排一个每天/每周固定时刻重复做的事（如"每天08:00叫我起床""每周一09:00提醒交周报"）。到点小王会自动去做。一次性的提醒（几分钟后/某个具体时刻一次）用 schedule_task，不要用这个。',
    paramSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: '短名字，如"吃药提醒"（也是之后取消时的标识）' },
        time: { type: 'string', description: "24小时制 'HH:MM'，如 08:30" },
        task: { type: 'string', description: '到点要做/提醒什么，一句话' },
        weekday: { type: 'integer', description: '0-6(0=周日)，只在这天触发；省略=每天' },
      },
      required: ['name', 'time', 'task'],
    },
    fn: tool_schedule_recurring,
    sideEffect: true,
  },
  {
    name: 'list_schedules',
    description: `列出当前所有定时安排：周期任务(每天/每周) + 待触发的一次性提醒。${IDENTITY.ownerName}问"你给我设了哪些定时的"，或你要取消/修改某个安排前，先用它拿到真实 id。`,
    paramSchema: { type: 'object', properties: {}, required: [] },
    fn: tool_list_schedules,
    sideEffect: false,
  },
  {
    name: 'cancel_schedule',
    description: '取消一个定时安排。type=recurring 取消周期任务、type=task 取消一次性提醒；id 用 list_schedules 给出的数字 id（别猜）。',
    paramSchema: {
      type: 'object',
      properties: {
        type: { type: 'string', enum: ['recurring', 'task'], description: 'recurring=周期任务，task=一次性提醒' },
        id: { type: 'integer', description: 'list_schedules 给出的 id' },
      },
      required: ['type', 'id'],
    },
    fn: tool_cancel_schedule,
    sideEffect: true,
  },
];

// ---- 渲染成 OpenAI tools 数组 ----
export function toOpenAITools(tools = TOOLS) {
  return tools.map((t) => ({
    type: 'function',
    function: { name: t.name, description: t.description, parameters: t.paramSchema },
  }));
}

// ---- 危险操作预检（callTool 在执行 fn 前调） ----
// 命中返回 {blocked:true, reason}，由 callTool 包成 {ok:false,error}，不抛穿。
function precheckDangerous(name, args) {
  if (name === 'query_db') {
    const sql = String(args?.sql ?? '');
    if (SQL_WRITE_RE.test(sql)) return { blocked: true, reason: 'query_db 只读，非 SELECT 被拦' };
  }
  if (name === 'http_get') {
    const url = String(args?.url ?? '');
    if (SSRF_PATTERNS.some((re) => re.test(url))) {
      return { blocked: true, reason: 'http_get 拦截内网/本地地址(SSRF 防护)' };
    }
  }
  return { blocked: false };
}

// =====================================================================
// callTool —— 统一执行入口（致命纪律①）
// =====================================================================
// 流程：查注册表 → dangerous 过黑名单 → sideEffect 先算 dedupHash 查/写去重 → 执行 fn
//      → catch 包成 {ok:false,error}（不抛穿，循环需 role:'tool' 回灌错误）。
// 注意：去重的"写"动作复用各工具自己的 INSERT OR IGNORE（send_message→enqueueOutbox、
//      schedule_task→createTask 的 idempotency_key），callTool 不再重复算一遍 outbox；
//      这里的统一 wrapper 价值在于：① 强制 dangerous 预检；② 统一异常包裹+证据日志；
//      ③ 把 deduped 标志透传给循环（让 LLM 知道"已派过"）。
export async function callTool(name, args, ctx = {}) {
  const tool = TOOLS.find((t) => t.name === name);
  if (!tool) {
    console.error('[tools] 未知工具: %s', name);
    return { ok: false, error: `未知工具: ${name}` };
  }

  // 参数防御：args 必须是对象
  const a = args && typeof args === 'object' ? args : {};

  // 危险操作黑名单（五层防御·工具校验层）
  if (tool.dangerous) {
    const chk = precheckDangerous(name, a);
    if (chk.blocked) {
      console.warn('[tools] %s 被黑名单拦截: %s', name, chk.reason);
      return { ok: false, error: `blocked: ${chk.reason}` };
    }
  }

  try {
    const result = await tool.fn(a, { db: ctx.db || getDb(), taskId: ctx.taskId ?? null, sessionId: ctx.sessionId ?? null, userInput: ctx.userInput ?? null });
    // 副作用工具的 deduped 标志透传（result 里自带）
    const deduped = result && typeof result === 'object' ? result.deduped === true : false;
    return { ok: true, result, deduped };
  } catch (err) {
    // 失败要响：留证据，但不抛穿——循环需要把错误以 role:'tool' 回灌给 LLM
    console.error('[tools] %s 执行失败: %s', name, err.message);
    return { ok: false, error: err.message };
  }
}

// ---- 小工具 ----
function clampInt(v, def, lo, hi) {
  const n = Number.parseInt(v, 10);
  if (!Number.isFinite(n)) return def;
  return Math.max(lo, Math.min(hi, n));
}

// ---- 自检：dedup 算法、沙箱穿越、黑名单、渲染（不联网、不依赖真 db） ----
const IS_MAIN =
  typeof process.argv[1] === 'string' && import.meta.url === pathToFileURL(process.argv[1]).href;

if (process.argv.includes('--selftest') && IS_MAIN) {
  let pass = 0,
    fail = 0;
  const ok = (c, m) => {
    console.log(`  ${c ? '✓' : '✗'} ${m}`);
    c ? pass++ : fail++;
  };
  console.log('tools.mjs selftest (纯逻辑，不建 db)\n');

  // dedupHash 稳定性（契约权威算法：空格拼接，与 durable/adapter/main 一致）
  const h1 = dedupHash(['wecom', 'OWNER', 'hi', 't1']);
  const h2 = dedupHash(['wecom', 'OWNER', 'hi', 't1']);
  ok(h1 === h2 && h1.length === 16, 'dedupHash 稳定且 16 字符');
  ok(dedupHash(['a', 'bc']) !== dedupHash(['ab', 'c']), '不同 parts → 不同 hash');

  // 沙箱穿越拦截
  let escaped = false;
  try {
    resolveSandboxPath('../../etc/passwd');
  } catch (e) {
    escaped = /sandbox escape/.test(e.message);
  }
  ok(escaped, '../ 越界被 resolveSandboxPath 拦');
  ok(resolveSandboxPath('a/b.txt').startsWith(SANDBOX_DIR + sep), '沙箱内路径正常解析');
  // 前缀同名目录不能绕过（SANDBOX_DIR + '-evil'）
  let prefixBlocked = false;
  try {
    resolveSandboxPath('../' + SANDBOX_DIR.split(sep).pop() + '-evil/x');
  } catch {
    prefixBlocked = true;
  }
  ok(prefixBlocked, '前缀同名目录(-evil)不能绕过沙箱');

  // 危险黑名单
  ok(precheckDangerous('query_db', { sql: 'DELETE FROM tasks' }).blocked, 'query_db 拦 DELETE');
  ok(!precheckDangerous('query_db', { sql: 'SELECT * FROM tasks' }).blocked, 'query_db 放行 SELECT');
  ok(precheckDangerous('http_get', { url: 'http://127.0.0.1/x' }).blocked, 'http_get 拦 127.0.0.1');
  ok(precheckDangerous('http_get', { url: 'http://192.168.1.1' }).blocked, 'http_get 拦私网 192.168');
  ok(!precheckDangerous('http_get', { url: 'https://example.com' }).blocked, 'http_get 放行外网');
  // 收紧后的 SSRF 覆盖：云元数据 / IP 编码绕过 / IPv6
  ok(precheckDangerous('http_get', { url: 'http://100.100.100.200/latest/meta-data/' }).blocked, 'http_get 拦阿里云元数据 100.100.100.200');
  ok(precheckDangerous('http_get', { url: 'http://2130706433/' }).blocked, 'http_get 拦十进制 IP(2130706433=127.0.0.1)');
  ok(precheckDangerous('http_get', { url: 'http://0x7f000001/' }).blocked, 'http_get 拦十六进制 IP(0x7f000001)');
  ok(precheckDangerous('http_get', { url: 'http://[::1]/x' }).blocked, 'http_get 拦 IPv6 回环 [::1]');
  ok(!precheckDangerous('http_get', { url: 'https://api.deepseek.com/v1' }).blocked, 'http_get 放行正常外网 API');

  // send_message 收件人锁定 owner：schema 不再暴露 target 参数（结构封死，非靠指令）
  const smTool = TOOLS.find((t) => t.name === 'send_message');
  ok(smTool && !smTool.paramSchema.properties.target, 'send_message schema 已移除 target 参数（收件人结构性锁定为 owner）');

  // OpenAI tools 渲染
  const rendered = toOpenAITools();
  ok(rendered.length === 15, '渲染出 15 个工具（13 + pin_fact + unpin_fact）');
  ok(rendered.every((t) => t.type === 'function' && t.function.name && t.function.parameters), '每个工具结构合法');
  ok(rendered.find((t) => t.function.name === 'send_message') != null, 'send_message 已注册');
  ok(rendered.find((t) => t.function.name === 'record_checkin') != null, 'record_checkin 已注册');
  ok(rendered.find((t) => t.function.name === 'pin_fact') != null, 'pin_fact 已注册');
  ok(rendered.find((t) => t.function.name === 'unpin_fact') != null, 'unpin_fact 已注册');
  for (const n of ['get_weather', 'schedule_recurring', 'list_schedules', 'cancel_schedule']) {
    ok(rendered.find((t) => t.function.name === n) != null, `${n} 已注册`);
  }
  // schedule_recurring 与 schedule_task 边界清晰（描述互指、不重叠 —— 原则11 crisp）
  const srTool = TOOLS.find((t) => t.name === 'schedule_recurring');
  ok(/一次性/.test(srTool.description) && /schedule_task/.test(srTool.description), 'schedule_recurring 描述划清与 schedule_task 的边界');

  // callTool 对未知工具/黑名单的返回（不抛穿）
  (async () => {
    const r1 = await callTool('not_exist', {});
    ok(r1.ok === false && /未知工具/.test(r1.error), 'callTool 未知工具返回 {ok:false}');
    const r2 = await callTool('query_db', { sql: 'DROP TABLE tasks' });
    ok(r2.ok === false && /blocked/.test(r2.error), 'callTool 黑名单命中返回 blocked（不抛穿）');

    console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
    process.exit(fail ? 1 : 0);
  })();
}
