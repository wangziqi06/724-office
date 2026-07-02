// =====================================================================
// adapter.mjs —— D 渠道适配与进程编排（adapter 层）
//
// 职责（契约 §模块接口契约 adapter.mjs）：
//   - relayOutbox()  : worker tick 调用，扫 outbox pending → 真发 → 标 sent
//   - sendWecom()    : 纯 fetch 调企微发消息 API，带 timeout；MOCK 时入内存 SENT
//   - enqueueOutbox(): send_message 工具底层，tx() 内 INSERT OR IGNORE（dedup_hash 去重）
//   - startAdapter() : CLI（stdin readline→runLoop→stdout）/ wecom（8090 http 回调→runLoop）
//   - 轮次装配层     : inboundText/inboundMedia → 安静窗口+下载栅栏攒齐"一轮"→ processTurn 一次回复
//                     （成串消息处理；边界原则「协议层 vs 对话层」见该节注释与 ARCHITECTURE.md）
//
// 三条致命纪律落地：
//   ① 副作用唯一出口是 outbox，enqueueOutbox 经 dedup_hash UNIQUE 去重（INSERT OR IGNORE）
//   ② 每个外部调用（sendWecom 的 fetch）都带 AbortController + timeout，绝不无限等待
//   ③ fetch（真发）在 tx 外，落库（status/sent_at）才进 tx，LLM/HTTP 不占 DB 锁
//
// 依赖方向（契约 §共享约定）：db ← adapter。只 import db.mjs，绝不反向 import loop/main。
// =====================================================================

import { createServer } from 'node:http';
import { createInterface } from 'node:readline';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { getDb, tx, nowMs } from './db.mjs';
import { handleMedia } from './media.mjs';
import { handleEsmInbound } from './esm.mjs';

// ---- 常量（契约 §常量集中：禁止魔法数散落）----
// HTTP/企微 timeout 复用 HTTP_TIMEOUT_MS；outbox 重试上限独立常量。
const HTTP_TIMEOUT_MS = Number(process.env.HTTP_TIMEOUT_MS || 15000);
const MAX_OUTBOX_ATTEMPTS = 5;
const HTTP_PORT = Number(process.env.HTTP_PORT || 8788);
let ADAPTER_MOCK = process.env.ADAPTER_MOCK === '1'; // let：selftest 自包含强制 mock，不依赖外部 env

// 企微发送凭证（契约 §环境变量）。沿用现有 e云企微(QIWE) doApi token 体系：
//   WECOM_API_URL = doApi 端点，WECOM_TOKEN = X-QIWEI-TOKEN，WECOM_GUID = 设备 guid。
// 若未来切官方企微（corpid/secret/agentid），在 sendWecom 内分支即可，对外签名不变。
const WECOM_API_URL = process.env.WECOM_API_URL || 'http://manager.qiweapi.com/qiwe/api/qw/doApi';
const WECOM_TOKEN = process.env.WECOM_TOKEN || '';
const WECOM_GUID = process.env.WECOM_GUID || '';
const WECOM_TARGET_ID = process.env.WECOM_TARGET_ID || ''; // owner 收件人 id（n=1）
// owner 门禁基准（n=1 只服务子淇）：优先 OWNER_ID，回落 WECOM_TARGET_ID。非 owner 入站直接丢。
const OWNER_ID = String(process.env.OWNER_ID || WECOM_TARGET_ID || '');
// 回调密钥路径：e云回调不签名，公网端口任何人可 POST → 配 secret 则 POST 必须命中 /cb/<secret>（防伪造注入·工具校验层）。
const WECOM_CALLBACK_SECRET = process.env.WECOM_CALLBACK_SECRET || '';
const CALLBACK_PATH = WECOM_CALLBACK_SECRET ? `/cb/${WECOM_CALLBACK_SECRET}` : null;
// 轮次装配·安静窗口：每来一条消息重置计时，窗口内没有新消息才认为"这一串说完了"（迁 live 4s）。
const DEBOUNCE_MS = parseInt(process.env.DEBOUNCE_MS || '4000', 10);
// 轮次装配·下载栅栏上限：安静窗口到了但媒体还在下载/识别时最多再等这么久（媒体卡死不无限扣住整轮）。
const FENCE_MAX_MS = parseInt(process.env.MEDIA_FENCE_MAX_MS || '30000', 10);
// msgType 分类（e云 cmd 15000）：文本 / 图片 / 语音。其它（文件等）首版静默放过（backlog）。
const TEXT_TYPES = new Set([0, 2]);
const IMAGE_TYPES = new Set([7, 14, 101]);
const VOICE_TYPES = new Set([16]);

// MOCK/selftest 下真发被拦截，发出的消息进这个内存数组，供断言去重（同 hash 只发一次）。
// 导出供 selftest 读取。watchdog 报警也复用 sendWecom，故这里集中持有。
export const SENT = [];

// relayOutbox 进程内单飞闸：worker tick(5s) 与 wecom adapter pollOutbox(10s) 是两个独立定时器、
// 同进程同事件循环，会在 await sendWecom 处交错读到同一 pending 行→重复发送。此布尔保证同一时刻
// 只有一个 relay 在跑，另一个直接早返回（被跳过的 pending 下一拍≤10s 必被消费，送达无实质延迟）。
let _relaying = false;

// ---------------------------------------------------------------------
// 带 timeout 的 fetch（致命纪律②）：任何外部调用绝不允许无限等待拖垮 worker tick。
// 超时抛 Error('timeout')，调用方计入证据（不静默吞）。
// ---------------------------------------------------------------------
async function fetchWithTimeout(url, opts = {}, timeoutMs = HTTP_TIMEOUT_MS, externalSignal = null) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new Error('timeout')), timeoutMs);
  // 外部 signal（如 watchdog 传入）与内部超时谁先触发都中止。
  if (externalSignal) {
    if (externalSignal.aborted) ctrl.abort(externalSignal.reason);
    else externalSignal.addEventListener('abort', () => ctrl.abort(externalSignal.reason), { once: true });
  }
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } catch (e) {
    // AbortError 在超时场景下统一报成 timeout，便于 callWithResilience / 日志识别。
    if (e.name === 'AbortError') { const err = new Error('timeout'); err.statusCode = 408; throw err; }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------
// sendWecom(target, content, {signal}) —— 纯 fetch 调企微发消息 API（契约签名）。
// 返回 { ok, error? }。失败不抛穿（relay 需要据返回值决定 attempts/last_error）。
//
// MOCK/selftest（ADAPTER_MOCK==='1'）：不真发，push 到内存 SENT 返回 ok:true。
// 为什么 push 到 SENT：selftest 断言"同 dedup_hash 只发一次"靠数 SENT.length。
// ---------------------------------------------------------------------

// 手机微信不渲染 Markdown：所有对外文本在真发出口统一剥离常见 md 符号（粗体/标题/代码/列表符），
// 避免 **/#/反引号在子淇手机上显示成难看的符号。这是结构层兜底——不指望模型每次都不吐 md（原则2）。
// 保守：只动明确是 md 语法的（粗体 **x**、行首标题井号、代码围栏/行内反引号、行首 -/* 列表符→•），不碰普通标点与中文。
export function stripMarkdownForPhone(text) {
  if (typeof text !== 'string' || !text) return text;
  return text
    .replace(/```[^\n]*\n?/g, '')             // 代码围栏行 → 去围栏，保留其中文字
    .replace(/`([^`]+)`/g, '$1')               // 行内代码 → 去反引号
    .replace(/\*\*(.+?)\*\*/gs, '$1')          // 粗体 **x** → x
    .replace(/^#{1,6}[ \t]+/gm, '')            // 行首标题井号 → 去掉
    .replace(/^([ \t]*)[-*][ \t]+/gm, '$1• '); // 行首 -/* 列表符 → •
}

export async function sendWecom(target, content, { signal = null } = {}) {
  content = stripMarkdownForPhone(content); // 出口统一剥 md，覆盖 reply/send_message/ESM/图片回执所有路径
  if (ADAPTER_MOCK) {
    SENT.push({ target: String(target), content });
    return { ok: true };
  }
  if (!WECOM_TOKEN || !WECOM_GUID) {
    // 凭证缺失：响亮报错，不假装成功（失败要响，团队原则）。
    return { ok: false, error: 'wecom credentials missing (WECOM_TOKEN/WECOM_GUID)' };
  }
  try {
    const r = await fetchWithTimeout(
      WECOM_API_URL,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8', 'X-QIWEI-TOKEN': WECOM_TOKEN },
        // e云企微 doApi 发文本格式（移植自 digital-twin/wecom_esm_bot.mjs qiweSendText）。
        body: JSON.stringify({ method: '/msg/sendText', params: { guid: WECOM_GUID, toId: String(target), content } }),
      },
      HTTP_TIMEOUT_MS,
      signal
    );
    const j = await r.json().catch(() => ({}));
    if (j.code !== 0 && j.errcode !== 0 && j.code !== undefined) {
      return { ok: false, error: `wecom api code=${j.code} msg=${j.msg || j.errmsg || ''}` };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ---------------------------------------------------------------------
// enqueueOutbox({channel,target,content,dedupHash}) —— send_message 工具底层（契约签名）。
// tx() 内 INSERT OR IGNORE outbox(dedup_hash UNIQUE)。受影响行数=0 → deduped=true（已派过，幂等）。
//
// 致命纪律①：副作用入队是唯一出口，dedupHash 由调用方按 conventions 算好传入（这里不重算，
// 保持职责单一——本函数只负责"按 hash 幂等入库"）。
// ---------------------------------------------------------------------
export function enqueueOutbox({ channel = 'wecom', target, content, dedupHash }) {
  if (!dedupHash) throw new Error('enqueueOutbox: dedupHash required');
  if (!target) throw new Error('enqueueOutbox: target required');
  return tx((db) => {
    const now = nowMs();
    const info = db
      .prepare(
        `INSERT OR IGNORE INTO outbox (channel, target, content, dedup_hash, status, attempts, created_at)
         VALUES (?, ?, ?, ?, 'pending', 0, ?)`
      )
      .run(channel, String(target), String(content), dedupHash, now);
    // changes=0 → 命中 UNIQUE，说明同 hash 已入队（尽力去重，at-least-once，不假装 effectively-once）。
    if (info.changes === 0) {
      const row = db.prepare('SELECT id FROM outbox WHERE dedup_hash = ?').get(dedupHash);
      return { id: row ? row.id : null, deduped: true };
    }
    return { id: Number(info.lastInsertRowid), deduped: false };
  });
}

// ---------------------------------------------------------------------
// relayOutbox() —— worker tick 调用（契约签名）。
// 扫 outbox status='pending'，逐条经 channel 真发：
//   成功 → tx() 置 status='sent'+sent_at；失败 → attempts+1+last_error，超上限置 'failed'。
//
// 致命纪律②③：fetch（真发）在 tx 外，仅落库在 tx 内（不占 DB 锁 + 不在事务里 await）。
// at-least-once：dedup_hash 已保证不重复入队；这里发送本身可能重试（崩在标 sent 前会重发，靠去重兜底）。
// ---------------------------------------------------------------------
export async function relayOutbox() {
  // 单飞闸：已有 relay 在跑就跳过本次（防跨定时器交错双发）。try/finally 保证异常路径也释放。
  if (_relaying) return { sent: 0, failed: 0, skipped: true };
  _relaying = true;
  try {
  const db = getDb();
  let sent = 0;
  let failed = 0;

  // 先把待发行读出（只读，不开事务），逐条在事务外发送，再单条落库。
  // 限制单 tick 处理量避免某 tick 过长（轻量机器，TICK_MS=5s）。
  const rows = db
    .prepare(`SELECT id, channel, target, content, attempts FROM outbox WHERE status = 'pending' ORDER BY created_at ASC LIMIT 50`)
    .all();

  for (const row of rows) {
    let result;
    if (row.channel === 'wecom') {
      result = await sendWecom(row.target, row.content); // ← fetch 在 tx 外（致命纪律③）
    } else {
      // 未知 channel：响亮失败，不静默吞，计入 attempts 走失败分支。
      result = { ok: false, error: `unknown channel: ${row.channel}` };
    }

    if (result.ok) {
      tx((d) => {
        d.prepare(`UPDATE outbox SET status = 'sent', sent_at = ? WHERE id = ?`).run(nowMs(), row.id);
      });
      sent++;
    } else {
      const attempts = row.attempts + 1;
      const dead = attempts >= MAX_OUTBOX_ATTEMPTS;
      tx((d) => {
        d.prepare(`UPDATE outbox SET attempts = ?, last_error = ?, status = ? WHERE id = ?`).run(
          attempts,
          String(result.error || 'unknown'),
          dead ? 'failed' : 'pending',
          row.id
        );
      });
      failed++;
      // 失败要响（留证据，不静默吞）。
      console.error('[adapter] outbox send failed id=%d attempts=%d: %s', row.id, attempts, result.error);
    }
  }

  return { sent, failed };
  } finally {
    _relaying = false;
  }
}

// ---------------------------------------------------------------------
// startAdapter(mode, {onMessage, pollOutbox}) —— 进程入站编排（契约职责）。
//
//   mode='cli'   : stdin readline → onMessage(text) → 把 reply 写 stdout（起步/本地调试）。
//   mode='wecom' : 8090(HTTP_PORT) http 回调 → onMessage(text) → runLoop；并定期 pollOutbox() relay。
//
// onMessage(payload) 由 main 注入 = handleIncoming 的薄包装，返回 reply 文本。
// pollOutbox() 由 main 注入 = relayOutbox 的薄包装（worker tick 已在 relay，这里 wecom 模式下
//   也兜一个低频 relay，保证回调进程独立存活时 outbox 仍被消费）。
//
// 返回 { stop } 句柄，便于优雅退出（main 注册 SIGTERM/SIGINT 时调用）。
// ---------------------------------------------------------------------
export function startAdapter(mode, { onMessage, pollOutbox = null } = {}) {
  if (typeof onMessage !== 'function') throw new Error('startAdapter: onMessage required');

  if (mode === 'cli') return startCliAdapter({ onMessage });
  if (mode === 'wecom') return startWecomAdapter({ onMessage, pollOutbox });
  throw new Error(`startAdapter: unknown mode '${mode}' (expected 'cli'|'wecom')`);
}

// ---- CLI adapter：stdin readline → runLoop → stdout（最小起步形态）----
function startCliAdapter({ onMessage }) {
  const rl = createInterface({ input: process.stdin, output: process.stdout, terminal: false });
  console.error('[adapter:cli] ready. type a line and press enter (ctrl-d to exit).');

  rl.on('line', async (line) => {
    const text = line.trim();
    if (!text) return;
    try {
      const reply = await onMessage({ sessionId: 'cli', userInput: text });
      process.stdout.write((reply ?? '') + '\n');
    } catch (e) {
      // 失败要响：把错误写 stderr，不假装回复成功。
      console.error('[adapter:cli] onMessage failed: %s', e.message);
      process.stdout.write('[error] ' + e.message + '\n');
    }
  });

  rl.on('close', () => console.error('[adapter:cli] stdin closed.'));

  return {
    mode: 'cli',
    stop() {
      rl.close();
    },
  };
}

// ---- 企微 adapter：HTTP 回调（P3 接）+ outbox relay 低频兜底 ----
// 回调进程读 POST body 提取文本 → onMessage → 把 reply 入 outbox（由 relay 真发，不在回调里阻塞发）。
// 为什么回调里不直接 sendWecom：回调要快返 200 给企微平台，发送走 outbox 异步 relay（解耦 + 去重 + 自愈）。
function startWecomAdapter({ onMessage, pollOutbox }) {
  // 轮次装配层的生产接线（selftest 用 mock deps 直测装配逻辑，见 inboundText/inboundMedia）。
  const turnDeps = {
    debounceMs: DEBOUNCE_MS,
    fenceMs: FENCE_MAX_MS,
    processMedia: (args) => handleMedia(args),
    esmInbound: (text, sessionId) => handleEsmInbound(getDb(), text, sessionId),
    sendReceipt: (target, content) => maybeReplyToOutbox(target, content),
    onMessage,
  };
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', service: 'xiaowang-v2-adapter' }));
      return;
    }
    if (req.method !== 'POST') {
      res.writeHead(405);
      res.end('method not allowed');
      return;
    }

    // 回调密钥路径校验（防伪造注入）：配了 secret 则 POST 必须命中 /cb/<secret>，否则 404。
    if (CALLBACK_PATH) {
      const pth = new URL(req.url, 'http://x').pathname;
      if (pth !== CALLBACK_PATH) { res.writeHead(404); res.end(); return; }
    }

    let body = '';
    req.on('data', (c) => {
      body += c;
      // 简单防超大 body（轻量机器自我保护），超 1MB 直接断。
      if (body.length > 1_000_000) req.destroy();
    });
    req.on('end', async () => {
      // 先快速 ack，避免企微平台重投（回调要求快返）。
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));

      let payload;
      try {
        payload = JSON.parse(body || '{}');
      } catch (e) {
        console.error('[adapter:wecom] bad json body: %s', e.message);
        return;
      }

      // 解析 e云 data 数组 → 自环过滤 + owner 门禁 → 全部模态进轮次装配层（攒齐一轮再交模型）。
      routeInbound(payload, {
        ownerId: OWNER_ID,
        onText: (senderId, text) => inboundText(senderId, text, turnDeps),
        onMedia: (senderId, msgData, kind) => inboundMedia(senderId, msgData, kind, turnDeps),
        onAccount: (d) => console.log('[adapter:wecom] account status:', d && d.code),
      });
    });
  });

  server.on('error', (e) => console.error('[adapter:wecom] server error: %s', e.message));
  server.listen(HTTP_PORT, '0.0.0.0', () => {
    console.error('[adapter:wecom] listening :%d (target=%s)', HTTP_PORT, WECOM_TARGET_ID ? 'set' : 'unset');
  });

  // outbox relay 兜底：回调进程独立存活时，低频 relay 消费 outbox。
  // worker tick 已在 relay（main.startWorker），这里是 wecom 进程的额外保险，间隔放宽到 10s。
  let relayTimer = null;
  if (typeof pollOutbox === 'function') {
    relayTimer = setInterval(async () => {
      try {
        await pollOutbox();
      } catch (e) {
        console.error('[adapter:wecom] pollOutbox failed: %s', e.message);
      }
    }, 10_000);
    relayTimer.unref?.(); // 不阻止进程退出
  }

  return {
    mode: 'wecom',
    server,
    stop() {
      if (relayTimer) clearInterval(relayTimer);
      server.close();
    },
  };
}

// =====================================================================
// 入站路由（e云 data 数组）：normalizeWecomMessages 拆包 → routeInbound 过滤分流。
// 抽成纯函数便于 selftest 断言；HTTP 回调与 selftest 共用同一逻辑。
// =====================================================================
function normalizeWecomMessages(payload) {
  if (!payload || typeof payload !== 'object') return [];
  if (payload.testMsg) return []; // e云连通性测试包，无业务消息
  let msgs = payload.data;
  if (!Array.isArray(msgs)) msgs = msgs ? [msgs] : [];
  // 兼容官方企微单对象（无 data 数组但有 MsgType/Content）
  if (msgs.length === 0 && (payload.MsgType || payload.content || payload.Content)) msgs = [payload];
  return msgs.filter((m) => m && typeof m === 'object');
}

// 路由：自环过滤(senderId===userId) + owner 门禁(非 owner 丢) + msgType 分流。
// onText(senderId,text) / onMedia(senderId,msgData,kind) / onAccount(msgData) 由调用方注入。
export function routeInbound(payload, { ownerId = OWNER_ID, onText, onMedia, onAccount } = {}) {
  for (const m of normalizeWecomMessages(payload)) {
    const cmd = m.cmd;
    const senderId = m.senderId != null ? m.senderId : extractWecomSender(m);
    const userId = m.userId;
    const msgType = m.msgType;
    const msgData = m.msgData && typeof m.msgData === 'object' ? m.msgData : m;

    // 自环过滤：自己发的（senderId===userId）跳过，不回自己。
    if (userId != null && senderId != null && String(senderId) === String(userId)) continue;
    // 账号状态等非消息 cmd
    if (cmd === 11016) { onAccount && onAccount(msgData); continue; }
    if (cmd != null && cmd !== 15000) continue;
    // owner 门禁（不信任缺失=owner，安全靠结构·团队原则3）：配了 ownerId 时，senderId 必须【显式等于】
    // ownerId 才放行；缺失或不等一律丢（n=1 只服务子淇，senderId 缺失=不可信，不再兜底当 owner）。
    // e云 doApi 回调恒带 senderId；这条只会挡掉伪造/无主消息，不影响正常收信。
    if (ownerId && String(senderId ?? '') !== String(ownerId)) continue;

    if (msgType == null || TEXT_TYPES.has(msgType)) {
      const text = (typeof msgData.content === 'string' ? msgData.content : '') || extractWecomText(m);
      if (text && onText) onText(String(senderId ?? ownerId), text);
    } else if (IMAGE_TYPES.has(msgType)) {
      onMedia && onMedia(String(senderId ?? ownerId), msgData, 'image');
    } else if (VOICE_TYPES.has(msgType)) {
      onMedia && onMedia(String(senderId ?? ownerId), msgData, 'voice');
    }
    // 其它 msgType（文件等）首版静默放过（backlog）。
  }
}

// =====================================================================
// 轮次装配层 —— 微信把主人的一个意思拆成一串独立到达的事件（连发短句/图配文/语音），
// 这里把"这一轮"机械攒齐，再交给模型一次理解、一次回复。
//
// 边界（原则11 + ARCHITECTURE.md「协议层 vs 对话层」）：
//   harness 只做【确定性装配】——安静窗口攒消息、下载栅栏等媒体就位、给每条记录
//   到达时刻/模态/顺序。"这串是一个意思还是几件事、图配哪句、是不是修正"这类模糊
//   判断全部留给模型，绝不在 loop 之前用规则判意图。
//   协议层例外：# 快记是显式语法，逐条抽出走确定性 fast-path；媒体失败即时确定性回执
//   （像朋友说"图挂了再发下"）；媒体成功不单独回执——回应由模型在整轮回复里一次说清。
//
// deps 注入（debounceMs/fenceMs/processMedia/esmInbound/sendReceipt/onMessage）：
//   生产接线在 startWecomAdapter（turnDeps）；selftest 传 mock deps + 短窗口离线直测。
// =====================================================================

const _turns = new Map();  // senderId → { items, pending, timer, windowDone, fenceTimer }
const _chains = new Map(); // senderId → Promise（轮次串行闸：上一轮回完才跑下一轮，回复不乱序）

const _sess = (senderId) => (senderId ? `wecom:${senderId}` : 'wecom');

function _getTurn(senderId) {
  let t = _turns.get(senderId);
  if (!t) {
    t = { items: [], pending: 0, timer: null, windowDone: false, fenceTimer: null, fenceDeadline: null };
    _turns.set(senderId, t);
  }
  return t;
}

// 每来一条消息重置安静窗口；栅栏【计时器】跟着撤（等窗口再到时重挂），
// 但栅栏【deadline】保留——它是硬上限，用户持续说话不能无限延长媒体等待（审查发现5）。
function _armWindow(senderId, deps) {
  const turn = _getTurn(senderId);
  clearTimeout(turn.timer);
  clearTimeout(turn.fenceTimer);
  turn.fenceTimer = null;
  turn.windowDone = false;
  const t = setTimeout(() => {
    turn.windowDone = true;
    _tryFlush(senderId, deps);
  }, deps.debounceMs);
  t.unref?.();
  turn.timer = t;
}

function _tryFlush(senderId, deps) {
  const turn = _turns.get(senderId);
  if (!turn || !turn.windowDone) return;
  if (turn.pending > 0) {
    // 下载栅栏：媒体还没就位不 flush（修"文字先跑 agent、看不到图"的竞态）。
    // deadline 只在首次进入等待时定死（绝对时刻），之后窗口怎么重置都不延长——上限兜底不无限等。
    if (turn.fenceDeadline == null) turn.fenceDeadline = nowMs() + deps.fenceMs;
    if (!turn.fenceTimer) {
      const ft = setTimeout(() => _flushTurn(senderId, deps), Math.max(0, turn.fenceDeadline - nowMs()));
      ft.unref?.();
      turn.fenceTimer = ft;
    }
    return;
  }
  _flushTurn(senderId, deps);
}

function _flushTurn(senderId, deps) {
  const turn = _turns.get(senderId);
  if (!turn) return;
  _turns.delete(senderId);
  clearTimeout(turn.timer);
  clearTimeout(turn.fenceTimer);
  // 栅栏超时仍未就位的媒体标记为孤儿：resolve 后作为新一轮的料送达，内容不丢。
  for (const it of turn.items) if (!it.ready) it.orphaned = true;
  const ready = turn.items.filter((it) => it.ready);
  const prev = _chains.get(senderId) || Promise.resolve();
  const next = prev
    .then(() => processTurn(senderId, ready, deps))
    .catch((e) => console.error('[adapter:wecom] processTurn failed: %s', e.message));
  _chains.set(senderId, next);
  next.finally(() => { if (_chains.get(senderId) === next) _chains.delete(senderId); });
}

// 文本入轮（导出供 selftest 离线直测）。
export function inboundText(senderId, text, deps) {
  _getTurn(senderId).items.push({ at: nowMs(), kind: 'text', text: String(text), ready: true, mediaLogId: null });
  _armWindow(senderId, deps);
}

// 媒体入轮：先占位（pending 栅栏计数），后台下载/识别/转写完成后原位填回——顺序按到达时刻保留。
export function inboundMedia(senderId, msgData, kind, deps) {
  const turn = _getTurn(senderId);
  const item = { at: nowMs(), kind, text: null, ready: false, mediaLogId: null, orphaned: false, turn };
  turn.items.push(item);
  turn.pending++;
  _armWindow(senderId, deps);
  // 两参 then（不是 .then().catch()）：catch 只兜 processMedia 的 rejection，
  // 绝不把 _resolveMediaItem 自身的异常再喂回 _resolveMediaItem（双 resolve → pending 双扣变负，审查发现3）。
  deps.processMedia({ senderId, sessionId: _sess(senderId), msgData, kind }).then(
    (out) => _resolveMediaItem(senderId, item, out, deps),
    (e) => {
      console.error('[adapter:wecom] media processing failed: %s', e.message);
      const receipt = kind === 'image' ? '图片没处理成，再发一次？' : '语音没处理成，再发一次？';
      _resolveMediaItem(senderId, item, { mediaLogId: null, receipt, feedText: null, desc: null }, deps);
    }
  );
}

function _resolveMediaItem(senderId, item, out, deps) {
  if (item.ready) return; // 幂等闸：同一 item 绝不二次 resolve（防未来改动引入双扣 pending 计数）
  // 成功：图→客观描述入轮，语音→转写文本入轮（打字等价）。失败：协议层即时回执，本轮不含这条。
  if (item.kind === 'image' && out && out.desc) {
    item.text = out.desc;
    item.mediaLogId = out.mediaLogId;
  } else if (item.kind === 'voice' && out && out.feedText) {
    item.text = out.feedText;
    item.mediaLogId = out.mediaLogId;
  } else {
    item.text = null;
    if (out && out.receipt && senderId) deps.sendReceipt(senderId, out.receipt);
  }
  item.ready = true;
  if (item.orphaned) {
    // 所属轮已被栅栏超时 flush 掉：迟到的内容作为新料进当前缓冲（紧接着的下一轮送达，不丢）。
    if (item.text != null) {
      _getTurn(senderId).items.push({ at: item.at, kind: item.kind, text: item.text, ready: true, mediaLogId: item.mediaLogId });
      _armWindow(senderId, deps);
    }
    return;
  }
  item.turn.pending--;
  if (_turns.get(senderId) === item.turn) _tryFlush(senderId, deps);
}

// 装配一轮的输入（纯函数，导出供 selftest 直测）。返回 { llmText, rawUserText }：
//   llmText     给模型看的。单条文字/语音原样直通（日常单发零脚手架零噪声、# 与打卡行为不变）；
//               多条则带 [HH:MM:SS 模态] 的到达时间事实——"一个意思还是几件事"由模型判断。
//   rawUserText 主人的纯原话（文字+语音转写，不含图片描述这类机器产物）：
//               供 record_checkin 等不可逆登记（esm_raw 绝不混入装配脚手架）+ 召回检索词。
export function buildTurnInput(items) {
  const live = (items || []).filter((it) => it && typeof it.text === 'string' && it.text.trim() !== '');
  if (!live.length) return { llmText: null, rawUserText: null };
  // 纯图轮 rawUserText='' 而非 null：'' 顺 ?? 传到 ctx.userInput，让 record_checkin 的空原话守卫生效——
  // 若回退成 userInput，机器产物（图片描述+脚手架）会被登记进 esm_raw 不可逆层（审查发现4）。
  const rawUserText = live.filter((it) => it.kind !== 'image').map((it) => it.text).join('\n');
  if (live.length === 1) {
    const it = live[0];
    if (it.kind !== 'image') return { llmText: it.text, rawUserText: it.text }; // 直通：与旧行为逐字一致
    return { llmText: `[图片 media#${it.mediaLogId ?? '?'}] ${it.text}`, rawUserText: '' };
  }
  const KIND_LABEL = { text: '文字', image: '图片', voice: '语音转写' };
  const fmt = (ms) => new Date(ms + 8 * 3600 * 1000).toISOString().slice(11, 19); // CST HH:MM:SS（与 esm cstShift 同口径）
  const lines = live.map((it) => {
    const tag = it.kind === 'image' ? `图片 media#${it.mediaLogId ?? '?'}` : (KIND_LABEL[it.kind] || it.kind);
    return `[${fmt(it.at)} ${tag}] ${it.text}`;
  });
  return { llmText: `【${live.length} 条连发消息｜按到达顺序】\n${lines.join('\n')}`, rawUserText };
}

// 一轮就绪：逐条过 ESM 显式语法 fast-path（# 混在连发里也逐条识别，不再被合并吞掉）→ 剩余装配跑 agent。
export async function processTurn(senderId, items, deps) {
  const sessionId = _sess(senderId);
  const rest = [];
  for (const it of items) {
    // 语音=打字等价，转写文本同样过 ESM 检查；图片描述是机器产物，不过。
    if (it.kind !== 'image') {
      try {
        const esm = await deps.esmInbound(it.text, sessionId);
        if (esm && esm.handled) {
          for (const r of esm.replies || []) if (r && senderId) await deps.sendReceipt(senderId, r);
          continue;
        }
      } catch (e) {
        // 拦截失败不能吞消息：记错误后落到 agent（宁可当普通聊天，也不丢主人的话）。
        console.error('[adapter:wecom] ESM intercept failed, fall through to agent: %s', e.message);
      }
    }
    rest.push(it);
  }
  const { llmText, rawUserText } = buildTurnInput(rest);
  if (!llmText) return;
  try {
    const reply = await deps.onMessage({ sessionId, userInput: llmText, rawUserInput: rawUserText });
    if (reply && senderId) await deps.sendReceipt(senderId, reply);
  } catch (e) {
    console.error('[adapter:wecom] processTurn onMessage failed: %s', e.message);
  }
}

// 回复/回执的 dedup_hash 掺【分钟桶】（导出供 selftest 直测）。
// 为什么：outbox 行永不删除，纯内容 hash 会让同一段固定文案（"记下了 ✓"、"图片没收完整，再发一次？"）
// 终身只发得出第一次——协议层反馈从第二次起被 INSERT OR IGNORE 静默吞掉（审查发现1）。
// 分钟桶保留"同一分钟内意外重复入队（如 e云回调重投）只发一次"的短窗保护，跨轮次相同文案照常送达。
export function replyDedupHash(target, content, at = nowMs()) {
  const norm = ['wecom', String(target), String(content), 'm' + Math.floor(at / 60000)]
    .map((p) => String(p ?? '').trim())
    .join(' ');
  return createHash('sha256').update(norm).digest('hex').slice(0, 16);
}

// 回调直发兜底：把 reply 入 outbox（dedup_hash 见 replyDedupHash）。
async function maybeReplyToOutbox(target, content) {
  try {
    enqueueOutbox({ channel: 'wecom', target, content, dedupHash: replyDedupHash(target, content) });
  } catch (e) {
    console.error('[adapter:wecom] enqueue reply failed: %s', e.message);
  }
}

// 从企微回调 payload 里提取文本。兼容 e云企微 doApi 回调 与 官方企微 xml-json 两种常见结构。
function extractWecomText(payload) {
  if (!payload || typeof payload !== 'object') return '';
  // e云企微：{ data: { content, msgType } } 或顶层 content
  if (payload.data && typeof payload.data.content === 'string' && (payload.data.msgType === 'text' || !payload.data.msgType)) {
    return payload.data.content;
  }
  if (typeof payload.content === 'string') return payload.content;
  // 官方企微：{ MsgType:'text', Content:'...' }
  if (payload.MsgType === 'text' && typeof payload.Content === 'string') return payload.Content;
  if (payload.Text && typeof payload.Text.Content === 'string') return payload.Text.Content;
  return '';
}

function extractWecomSender(payload) {
  if (!payload || typeof payload !== 'object') return '';
  if (payload.data && (payload.data.fromId || payload.data.senderId)) return String(payload.data.fromId || payload.data.senderId);
  if (payload.FromUserName) return String(payload.FromUserName);
  if (payload.fromId || payload.senderId) return String(payload.fromId || payload.senderId);
  return '';
}

// =====================================================================
// --selftest：离线（ADAPTER_MOCK）验证 outbox 入队去重 + relay 真发只发一次。
// 不联网、不真发；用临时 db。断言风格 ok(cond,msg) 打 ✓/✗，结尾 PASS/FAIL 计数，退出码=fail?1:0。
// =====================================================================
async function selftest() {
  // 必须在 import db.mjs 前不可能改 env（top-level import 已固化），故 selftest 用临时 db 路径需提前设。
  // 这里假定运行命令已设 XW2_DB_PATH 指向 scratchpad 临时文件 + ADAPTER_MOCK=1（见文件头注释）。
  const { initDb } = await import('./db.mjs');
  initDb(); // 幂等建表
  ADAPTER_MOCK = true; // selftest 强制 mock：自包含、不真发、不依赖外部 env（与其它模块自检一致）

  let pass = 0;
  let fail = 0;
  const ok = (cond, msg) => {
    if (cond) { pass++; console.log('  ✓ ' + msg); }
    else { fail++; console.log('  ✗ ' + msg); }
  };

  const { createHash } = await import('node:crypto');
  const dedup = (parts) => createHash('sha256').update(parts.map((p) => String(p ?? '').trim()).join(' ')).digest('hex').slice(0, 16);

  console.log('adapter selftest (ADAPTER_MOCK=%s)\n', ADAPTER_MOCK ? '1' : '0');
  ok(ADAPTER_MOCK, 'ADAPTER_MOCK=1 (selftest must not really send)');

  // 1) enqueueOutbox：首次入队 deduped=false，相同 hash 再入队 deduped=true。
  const h1 = dedup(['wecom', 'owner1', 'hello world', '']);
  const r1 = enqueueOutbox({ channel: 'wecom', target: 'owner1', content: 'hello world', dedupHash: h1 });
  ok(r1.deduped === false && r1.id, 'enqueueOutbox first insert (deduped=false, id set)');
  const r2 = enqueueOutbox({ channel: 'wecom', target: 'owner1', content: 'hello world', dedupHash: h1 });
  ok(r2.deduped === true, 'enqueueOutbox same dedup_hash → deduped=true (致命纪律①)');

  // 2) relayOutbox：pending 只有 1 条（去重后），真发（mock）入 SENT 一次，行被标 sent。
  SENT.length = 0;
  const relayed = await relayOutbox();
  ok(relayed.sent === 1 && relayed.failed === 0, 'relayOutbox sent=1 failed=0');
  ok(SENT.length === 1 && SENT[0].content === 'hello world', 'sendWecom (mock) pushed exactly once → at-least-once+去重生效');

  // 3) 再 relay 一次：已 sent，无 pending，不重发。
  SENT.length = 0;
  const relayed2 = await relayOutbox();
  ok(relayed2.sent === 0 && SENT.length === 0, 'second relay sends nothing (status=sent, not re-sent)');

  // 4) 不同内容 → 不同 hash → 真发一次。
  const h2 = dedup(['wecom', 'owner1', 'second message', '']);
  enqueueOutbox({ channel: 'wecom', target: 'owner1', content: 'second message', dedupHash: h2 });
  SENT.length = 0;
  const relayed3 = await relayOutbox();
  ok(relayed3.sent === 1 && SENT.length === 1, 'distinct content → new hash → sent once');

  // 5) sendWecom mock 返回 ok:true 且入 SENT。
  SENT.length = 0;
  const sw = await sendWecom('owner1', 'direct', {});
  ok(sw.ok === true && SENT.length === 1, 'sendWecom mock returns ok and records SENT');

  // 5.5) 单飞闸：并发两个 relayOutbox 不重复发送（修双发竞态）。
  //   Promise.all 同时发起 A/B：A 先同步跑到首个 await sendWecom 置 _relaying，B 紧接着看到 _relaying 直接 skip。
  SENT.length = 0;
  enqueueOutbox({ channel: 'wecom', target: 'owner1', content: 'concA', dedupHash: dedup(['wecom', 'owner1', 'concA', '']) });
  enqueueOutbox({ channel: 'wecom', target: 'owner1', content: 'concB', dedupHash: dedup(['wecom', 'owner1', 'concB', '']) });
  const [ra, rb] = await Promise.all([relayOutbox(), relayOutbox()]);
  ok(SENT.length === 2, '并发 relay：两条各发一次（不是四次）—— 单飞闸消除跨定时器双发');
  ok(ra.skipped === true || rb.skipped === true, '并发其中一个 relay 被单飞闸跳过(skipped=true)');

  // 6) routeInbound：e云 data 数组路由 + 自环过滤 + owner 门禁 + msgType 分流。
  const got = { texts: [], media: [] };
  routeInbound(
    {
      data: [
        { cmd: 15000, msgType: 0, senderId: 'OWNER', userId: 'BOT', msgData: { content: '你好' } },
        { cmd: 15000, msgType: 0, senderId: 'BOT', userId: 'BOT', msgData: { content: '自己发的应跳过' } },
        { cmd: 15000, msgType: 7, senderId: 'OWNER', userId: 'BOT', msgData: { fileId: 'i' } },
        { cmd: 15000, msgType: 16, senderId: 'OWNER', userId: 'BOT', msgData: { fileId: 'v' } },
        { cmd: 15000, msgType: 0, senderId: 'STRANGER', userId: 'BOT', msgData: { content: '陌生人' } },
        { cmd: 11016, senderId: 'OWNER', userId: 'BOT', msgData: { code: 0 } },
      ],
    },
    {
      ownerId: 'OWNER',
      onText: (s, t) => got.texts.push([s, t]),
      onMedia: (s, d, k) => got.media.push([s, k]),
    },
  );
  ok(got.texts.length === 1 && got.texts[0][1] === '你好', 'routeInbound 只路由 OWNER 的一条文本');
  ok(got.media.some((m) => m[1] === 'image') && got.media.some((m) => m[1] === 'voice'), 'routeInbound 图片/语音分流到 onMedia');
  ok(!got.texts.some((t) => /自己发的|陌生人/.test(t[1])), 'routeInbound 跳过自环(senderId===userId)+非owner');

  // 7) buildTurnInput：装配纯函数（直通 / 时间事实 / 原话分离）
  const T0 = Date.UTC(2026, 6, 1, 6, 32, 5); // = 14:32:05 CST
  const single = buildTurnInput([{ at: T0, kind: 'text', text: '帮我看下日程' }]);
  ok(single.llmText === '帮我看下日程' && single.rawUserText === '帮我看下日程', 'buildTurnInput 单条文字原样直通（零脚手架，G 场景不变）');
  const singleVoice = buildTurnInput([{ at: T0, kind: 'voice', text: '睡得还行' }]);
  ok(singleVoice.llmText === '睡得还行' && singleVoice.rawUserText === '睡得还行', 'buildTurnInput 单条语音转写直通（打字等价，ESM 打卡行为不变）');
  const multi = buildTurnInput([
    { at: T0, kind: 'text', text: '帮我看看这个' },
    { at: T0 + 3000, kind: 'image', text: '一盘沙拉', mediaLogId: 41 },
    { at: T0 + 9000, kind: 'text', text: '中午吃这个够吗' },
  ]);
  ok(
    /【3 条连发消息/.test(multi.llmText) &&
    multi.llmText.includes('[14:32:05 文字] 帮我看看这个') &&
    multi.llmText.includes('[14:32:08 图片 media#41] 一盘沙拉') &&
    multi.llmText.includes('[14:32:14 文字] 中午吃这个够吗'),
    'buildTurnInput 多条带到达时刻/模态/顺序（CST，时序事实给模型判断）'
  );
  ok(multi.rawUserText === '帮我看看这个\n中午吃这个够吗', 'rawUserText=纯原话（文字+语音转写），不含图片描述——不可逆层不被脚手架污染');
  const imgOnly = buildTurnInput([{ at: T0, kind: 'image', text: '一张图', mediaLogId: 5 }]);
  ok(imgOnly.llmText === '[图片 media#5] 一张图' && imgOnly.rawUserText === '', '纯图轮 rawUserText=空串——顺 ?? 传到 ctx 触发 record_checkin 空守卫，机器产物进不了 esm_raw（审查发现4）');

  // 7.5) replyDedupHash 分钟桶：同分钟同 hash（短窗防重投），跨分钟不同 hash（固定文案回执不被终身 dedup 吞掉，审查发现1）
  const atA = Date.UTC(2026, 6, 2, 6, 0, 10);
  ok(replyDedupHash('owner1', '记下了 ✓', atA) === replyDedupHash('owner1', '记下了 ✓', atA + 20_000), 'replyDedupHash 同分钟同 hash（保留回调重投保护）');
  ok(replyDedupHash('owner1', '记下了 ✓', atA) !== replyDedupHash('owner1', '记下了 ✓', atA + 120_000), 'replyDedupHash 跨分钟不同 hash（固定回执跨轮次照常送达）');

  // 8) 装配层集成：文字连发攒成一轮 → onMessage 只跑一次、一条回复
  const calls = [];
  const receipts = [];
  const mkDeps = (over = {}) => ({
    debounceMs: 40,
    fenceMs: 200,
    processMedia: async () => ({ mediaLogId: 9, receipt: null, feedText: null, desc: 'mock图' }),
    esmInbound: async (text) => (text.startsWith('#') && text.slice(1).trim() ? { handled: true, replies: ['记下了 ✓'] } : { handled: false }),
    sendReceipt: async (t, c) => { receipts.push(c); },
    onMessage: async ({ sessionId, userInput, rawUserInput }) => { calls.push({ sessionId, userInput, rawUserInput }); return 'ok回复'; },
    ...over,
  });
  const deps8 = mkDeps();
  inboundText('S8', '第一条', deps8);
  inboundText('S8', '第二条', deps8);
  await new Promise((r) => setTimeout(r, 150));
  ok(calls.length === 1 && /第一条/.test(calls[0].userInput) && /第二条/.test(calls[0].userInput), '连发两条文字攒成一轮，onMessage 只跑一次');
  ok(/【2 条连发消息/.test(calls[0].userInput), '多条装配带时间结构头');
  ok(calls[0].rawUserInput === '第一条\n第二条', 'rawUserInput=纯原话透传');
  ok(receipts.includes('ok回复'), '一轮一条回复入 outbox');

  // 9) 下载栅栏：文字先到、图还在识别 → 窗口到仍扣住，图就位后同轮给模型（修竞态）
  calls.length = 0; receipts.length = 0;
  let resolveImg;
  const deps9 = mkDeps({ processMedia: () => new Promise((r) => { resolveImg = r; }) });
  inboundText('S9', '看看这个', deps9);
  inboundMedia('S9', { fileId: 'x' }, 'image', deps9);
  await new Promise((r) => setTimeout(r, 120)); // 窗口(40ms)已过，图未就位
  ok(calls.length === 0, '窗口到但媒体未就位 → 栅栏扣住不 flush（修"文字先跑 agent 看不到图"竞态）');
  resolveImg({ mediaLogId: 7, receipt: null, feedText: null, desc: '一碗牛肉面' });
  await new Promise((r) => setTimeout(r, 60));
  ok(calls.length === 1 && /牛肉面/.test(calls[0].userInput) && /看看这个/.test(calls[0].userInput), '媒体就位后一轮 flush：图描述与文字同轮');
  ok(!receipts.some((c) => /📷/.test(c)), '图片成功不再发独立 📷 回执（对话层边界）');

  // 10) 栅栏超时：媒体卡死不无限扣轮；迟到内容作为新一轮送达不丢
  calls.length = 0; receipts.length = 0;
  let resolveLate;
  const deps10 = mkDeps({ processMedia: () => new Promise((r) => { resolveLate = r; }) });
  inboundText('S10', '先说句话', deps10);
  inboundMedia('S10', { fileId: 'y' }, 'image', deps10);
  await new Promise((r) => setTimeout(r, 350)); // 窗口40 + 栅栏200 之后
  ok(calls.length === 1 && /先说句话/.test(calls[0].userInput), '栅栏超时 → 先 flush 已就位的（不无限等）');
  resolveLate({ mediaLogId: 8, receipt: null, feedText: null, desc: '迟到的图' });
  await new Promise((r) => setTimeout(r, 120));
  ok(calls.length === 2 && /迟到的图/.test(calls[1].userInput), '迟到媒体作为新一轮送达（内容不丢）');

  // 11) # 快记混在连发里逐条抽出（旧版 \n 合并后 # 被吞）
  calls.length = 0; receipts.length = 0;
  const deps11 = mkDeps();
  inboundText('S11', '#中午跑了5km', deps11);
  inboundText('S11', '对了明天提醒我带伞', deps11);
  await new Promise((r) => setTimeout(r, 150));
  ok(receipts.includes('记下了 ✓'), '# 快记逐条走确定性 fast-path（混在连发里不丢）');
  ok(calls.length === 1 && calls[0].userInput === '对了明天提醒我带伞', '剩余单条直通 agent（不带脚手架）');

  // 12) 轮次串行：上一轮处理中来的新消息排队为下一轮，回复顺序不乱、追发不丢
  calls.length = 0; receipts.length = 0;
  let releaseFirst;
  const deps12 = mkDeps({
    onMessage: async ({ userInput }) => {
      calls.push({ userInput });
      if (calls.length === 1) await new Promise((r) => { releaseFirst = r; });
      return 'r' + calls.length;
    },
  });
  inboundText('S12', '第一轮', deps12);
  await new Promise((r) => setTimeout(r, 80)); // 第一轮 flush，onMessage 挂起
  inboundText('S12', '第二轮', deps12);
  await new Promise((r) => setTimeout(r, 80)); // 第二轮窗口到，进串行链排队
  ok(calls.length === 1, '上一轮未完成时新一轮排队（同 sender 不并发跑 agent）');
  releaseFirst();
  await new Promise((r) => setTimeout(r, 80));
  ok(calls.length === 2 && calls[1].userInput === '第二轮', '第二轮在第一轮完成后按序跑（追发不丢、回复不乱序）');

  // 13) 媒体失败：协议层即时确定性回执，不进装配轮
  calls.length = 0; receipts.length = 0;
  const deps13 = mkDeps({ processMedia: async () => ({ mediaLogId: null, receipt: '图片没收完整，再发一次？', feedText: null, desc: null }) });
  inboundMedia('S13', {}, 'image', deps13);
  await new Promise((r) => setTimeout(r, 150));
  ok(receipts.includes('图片没收完整，再发一次？'), '媒体失败 → 协议层即时回执（不等栅栏）');
  ok(calls.length === 0, '失败媒体不进装配轮（不产生空 agent 轮）');

  // 14) 栅栏是硬上限：用户持续说话不延长媒体等待（deadline 首次进入等待即定死，审查发现5）
  calls.length = 0; receipts.length = 0;
  const deps14 = mkDeps({ debounceMs: 50, fenceMs: 150, processMedia: () => new Promise(() => {}) }); // 媒体永不就位
  inboundText('S14', '开头', deps14);
  inboundMedia('S14', { fileId: 'z' }, 'image', deps14);
  await new Promise((r) => setTimeout(r, 120)); // 窗口50ms已到 → 栅栏 deadline≈t50+150=t200 定死
  inboundText('S14', '还在说', deps14);         // 窗口重置（未修复版会把栅栏重新起算到 ~t320）
  await new Promise((r) => setTimeout(r, 150)); // 现在 ≈t270：deadline t200 已过 → 必须已 flush
  ok(calls.length === 1 && /开头/.test(calls[0].userInput) && /还在说/.test(calls[0].userInput),
     '栅栏硬上限：窗口重置不延长媒体等待，超时照 flush 已就位的两条文字');

  console.log(`\nPASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

// CLI 入口（契约：import.meta.url === pathToFileURL(process.argv[1]).href）。
if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  if (process.argv.includes('--selftest')) {
    selftest();
  } else {
    // 直接运行 adapter.mjs（无 main 编排）= 起一个最小 CLI adapter，回声 onMessage 不接 loop。
    // 真实部署由 main.mjs startAdapter 注入 onMessage=handleIncoming。
    console.error('[adapter] standalone CLI echo mode (no loop). use main.mjs for full agent.');
    startAdapter('cli', {
      onMessage: async ({ userInput }) => `echo: ${userInput}`,
    });
  }
}
