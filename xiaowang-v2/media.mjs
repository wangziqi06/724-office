// =====================================================================
// media.mjs —— 媒体统一为可召回的一等上下文（P2）
//
// 两层架构（迁 live esm-bot 已实证链路，非发明）：
//   ① 不可逆原始字节落盘（MEDIA_DIR/…）—— 原则9 绝不自动删
//   ② 可再生文本（图走 kimi 视觉描述 / 语音走讯飞 ASR）—— 失败可重跑
// 入库：media_log（两层）+ 图片描述另写普通 episode（role='user', entity='media',
//   content='[media#N|图片] …', task_id=media_log.id）→ 只作 retrieveMedia 的召回索引；
//   逐字近窗那份由轮次装配的 user episode 承担（context.recentVerbatim 已滤 entity='media' 防双写）。
// 语音=打字等价：转写出文本后交回 adapter 的 onMessage 当普通消息走主链路（不另写 media episode，
//   避免与 onMessage 落的 user 轮重复）；原始音频始终落 media_log（零丢失）。
//
// 失败要响（不静默 transcript=null）：ffmpeg 缺 / 未配 ASR / 转写失败都给【明确回执】+ 留原始音频。
// 资源纪律（2C2G）：ASR/ffmpeg 串行队列，一次只跑一个；外部调用全带 timeout。
//
// 依赖方向（单向无环）：db/memory/asr ← media。绝不 import adapter（回执由 adapter 发，本模块只返数据）。
// 可整块删除（原则6）：删本文件 + adapter 媒体分流即可退回纯文本。
// =====================================================================

import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

import { getDb, tx, nowMs } from './db.mjs';
import { appendEpisode } from './memory.mjs';
import { transcribeVoice as xfyunTranscribe } from './asr.mjs';

const DIR = import.meta.dirname;

// ---- env（自带 loader，与 llm.mjs 一致：.env 覆盖 process.env） ----
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

// e云企微媒体下载（复用 adapter 同一套 WECOM_* 凭证；下载走 doApi X-QIWEI-TOKEN）。
const WECOM_API_URL = ENV.WECOM_API_URL || 'http://manager.qiweapi.com/qiwe/api/qw/doApi';
const WECOM_TOKEN = ENV.WECOM_TOKEN || '';
const WECOM_GUID = ENV.WECOM_GUID || '';
const HTTP_TIMEOUT_MS = parseInt(ENV.HTTP_TIMEOUT_MS || '15000', 10);
// 视觉(图像识别)：OpenAI 兼容 chat/completions，默认 kimi 视觉。
const VISION_API_URL = ENV.VISION_API_URL || 'https://api.moonshot.cn/v1/chat/completions';
const VISION_API_KEY = ENV.VISION_API_KEY || '';
const VISION_MODEL = ENV.VISION_MODEL || 'kimi-k2.5';
// 语音 ASR：VOICE_ASR=xfyun 启用讯飞；留空=只存音频不转写（原始音频仍落盘，可后补转）。
const VOICE_ASR = ENV.VOICE_ASR || '';
const MEDIA_DIR = ENV.MEDIA_DIR ? ENV.MEDIA_DIR : join(DIR, 'media');
// 媒体字节上限（防 1.6G 机 OOM + 同步 base64 编码阻塞 event loop 拖垮 worker tick/心跳）。默认 20MB。
const MAX_MEDIA_BYTES = parseInt(ENV.MAX_MEDIA_BYTES || String(20 * 1024 * 1024), 10);

// 媒体下载 URL 的 SSRF 兜底（path3 fileHttpUrl 来自回调、可控）：拦内网/链路本地/云元数据/IPv6。
// 本地极简版（避免 media→tools 形成 import 环）；正常 qiwe CDN URL 不受影响。
function isBlockedUrl(u) {
  return /^https?:\/\/(localhost|127\.|0\.0\.0\.0|169\.254\.|100\.100\.100\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[)/i.test(String(u || ''));
}

// ---- 带 timeout 的 fetch（致命纪律②） ----
async function fetchT(url, opts = {}, timeoutMs = HTTP_TIMEOUT_MS) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(new Error('timeout')), timeoutMs);
  try {
    return await fetch(url, { ...opts, signal: ac.signal });
  } finally {
    clearTimeout(timer);
  }
}

// ---- e云 doApi 通用调用（媒体下载用） ----
async function qiweApi(method, params) {
  const r = await fetchT(WECOM_API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'X-QIWEI-TOKEN': WECOM_TOKEN },
    body: JSON.stringify({ method, params: { guid: WECOM_GUID, ...params } }),
  });
  // e云网关偶发返非 JSON(502 HTML/限流页)：不让 r.json() 抛穿(会跳过下载 try、用户连"没收到"回执都没有)。
  // 返结构化错误，让上层走 cloudUrl='' → null → "再发一次"回执（失败要响，团队 BLUEPRINT）。
  try {
    return await r.json();
  } catch (e) {
    console.error('[media] qiweApi 非 JSON 响应 (%s, http %s): %s', method, r.status, e.message);
    return { code: -1, msg: '非JSON响应' };
  }
}

// ---- 文件类型判定 ----
function imageMime(buf) {
  if (buf[0] === 0xff && buf[1] === 0xd8) return 'image/jpeg';
  if (buf[0] === 0x89 && buf[1] === 0x50) return 'image/png';
  if (buf[0] === 0x47 && buf[1] === 0x49) return 'image/gif';
  if (buf.length > 11 && buf.slice(0, 4).toString('latin1') === 'RIFF' && buf.slice(8, 12).toString('latin1') === 'WEBP') return 'image/webp';
  return 'image/jpeg';
}
function mediaExt(buf, kind) {
  if (kind === 'image') return '.' + imageMime(buf).split('/')[1].replace('jpeg', 'jpg');
  const head = buf.slice(0, 12).toString('latin1'); // 企微语音=AMR；微信可能 SILK
  if (head.includes('SILK')) return '.silk';
  return '.amr';
}

// ---- 下载企微媒体 → 落盘，返回本地路径（原始字节，不可逆）。三路兜底，对齐 live qiwe.py ----
async function downloadMedia(msgData, kind, { mock = false } = {}) {
  if (mock) {
    const md = process.env.MEDIA_DIR || MEDIA_DIR; // selftest 期经 env 指 temp，避免污染仓库目录
    if (!existsSync(md)) mkdirSync(md, { recursive: true });
    const fp = join(md, `mock_${kind}.bin`);
    if (!existsSync(fp)) writeFileSync(fp, Buffer.from(kind === 'image' ? [0xff, 0xd8, 0xff] : '#!SILK_V3'));
    return fp;
  }
  const fileType = kind === 'image' ? 1 : 5; // e云: 1=图片 5=语音/文件
  const fileId = msgData.fileId || '';
  const fileAesKey = msgData.fileAeskey || msgData.fileAesKey || '';
  const fileAuthKey = msgData.fileAuthkey || msgData.fileAuthKey || '';
  const fileSize = msgData.fileSize || msgData.fileBigSize || 0;
  // 大小卡口①：回调已声明大小超阈值 → 直接拒收，连下载都不发起（最省资源）。
  if (Number(fileSize) > MAX_MEDIA_BYTES) {
    console.error('[media] declared size %sB exceeds cap %dB, reject', fileSize, MAX_MEDIA_BYTES);
    return null;
  }
  let cloudUrl = '';
  if (fileId && fileAesKey) { // 路径1: 企微格式(有 fileId)
    const j = await qiweApi('/cloud/wxWorkDownload', { fileAeskey: fileAesKey, fileId, fileSize, fileType });
    if (j.code === 0 && j.data?.cloudUrl) cloudUrl = j.data.cloudUrl;
    else console.error('[media] wxWorkDownload', j.code, j.msg);
  }
  if (!cloudUrl && fileAuthKey) { // 路径2: 个微格式(有 authKey)
    const fileUrl = msgData.fileBigHttpUrl || msgData.fileMiddleHttpUrl || msgData.fileThumbHttpUrl || '';
    if (fileUrl) {
      const j = await qiweApi('/cloud/wxDownload', { fileAeskey: fileAesKey, fileAuthkey: fileAuthKey, fileUrl, fileSize, fileType });
      if (j.code === 0 && j.data?.cloudUrl) cloudUrl = j.data.cloudUrl;
      else console.error('[media] wxDownload', j.code, j.msg);
    }
  }
  if (!cloudUrl) cloudUrl = msgData.fileHttpUrl || msgData.fileUrl || ''; // 路径3: 直接 HTTP 兜底
  if (!cloudUrl) { console.error('[media] no url, keys=', Object.keys(msgData).join(',')); return null; }
  // SSRF 兜底：path3 的 fileHttpUrl/fileUrl 来自回调可控，拦内网/元数据地址。
  if (isBlockedUrl(cloudUrl)) { console.error('[media] blocked internal/metadata media url (SSRF 防护)'); return null; }
  try {
    const resp = await fetchT(cloudUrl);
    // 大小卡口②：Content-Length 超阈值 → 不读体直接弃（防无 fileSize 声明时仍 OOM）。
    const clen = Number(resp.headers.get('content-length') || 0);
    if (clen > MAX_MEDIA_BYTES) { console.error('[media] content-length %dB exceeds cap %dB, abort', clen, MAX_MEDIA_BYTES); return null; }
    const buf = Buffer.from(await resp.arrayBuffer());
    // 大小卡口③：无 Content-Length 时的最后兜底（已读入但不落盘/不编码，避免后续 base64 阻塞）。
    if (buf.length > MAX_MEDIA_BYTES) { console.error('[media] downloaded %dB exceeds cap %dB, drop', buf.length, MAX_MEDIA_BYTES); return null; }
    if (!existsSync(MEDIA_DIR)) mkdirSync(MEDIA_DIR, { recursive: true });
    const rnd = nowMs().toString(36) + '-' + buf.length.toString(36);
    const fp = join(MEDIA_DIR, `${rnd}_${kind}${mediaExt(buf, kind)}`);
    writeFileSync(fp, buf);
    console.log(`[media] saved ${kind} ${buf.length}B -> ${fp}`);
    return fp;
  } catch (e) { console.error('[media] fetch/save', e.message); return null; }
}

// ---- 图像 → kimi 视觉客观描述（可再生层）。5MB 上限 ----
async function describeImage(fp, { mock = false } = {}) {
  if (mock) return '一碗牛肉面配煎蛋和青菜(mock)';
  if (!VISION_API_KEY) return null;
  const buf = readFileSync(fp);
  if (buf.length > 5 * 1024 * 1024) { console.error('[media] image >5MB skip'); return null; }
  const dataUrl = `data:${imageMime(buf)};base64,${buf.toString('base64')}`;
  try {
    const r = await fetchT(VISION_API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${VISION_API_KEY}` },
      body: JSON.stringify({ model: VISION_MODEL, max_tokens: 1024, messages: [{ role: 'user', content: [
        { type: 'image_url', image_url: { url: dataUrl } },
        { type: 'text', text: '客观记录这张图里能看到的：物体/文字/数量/场景。若是食物，逐项说清有哪些食物饮料。只描述，不评价、不猜健康影响。一段话。' },
      ] }] }),
    }, 60000);
    const j = await r.json();
    if (j.error) { console.error('[media][vision]', j.error.message); return null; }
    return (j.choices?.[0]?.message?.content || '').trim() || null;
  } catch (e) { console.error('[media][vision]', e.message); return null; }
}

// ---- ffmpeg 可用性探测（懒探测，缓存）。缺 ffmpeg → 语音降级只存音频（用户已选「迁但可降级」）----
let _ffmpegOk = null;
function probeFfmpeg() {
  return new Promise((res) => {
    try {
      const ff = spawn('ffmpeg', ['-version']);
      ff.on('error', () => res(false));
      ff.on('close', (code) => res(code === 0));
    } catch { res(false); }
  });
}
async function ffmpegOk() {
  if (_ffmpegOk === null) {
    _ffmpegOk = await probeFfmpeg();
    if (!_ffmpegOk) console.warn('[media] ffmpeg 不可用：语音转写降级为只存音频（装 ffmpeg 后自动恢复）');
  }
  return _ffmpegOk;
}

// ---- ASR 串行队列（2C2G：一次只跑一个 ffmpeg/讯飞，避免抢内存/带宽） ----
let _asrChain = Promise.resolve();
function enqueueAsr(fp) {
  const run = () => xfyunTranscribe(fp, { appId: ENV.XFYUN_APP_ID, apiKey: ENV.XFYUN_API_KEY, apiSecret: ENV.XFYUN_API_SECRET });
  const p = _asrChain.then(run, run); // 不论上一个成败都接着跑
  _asrChain = p.then(() => {}, () => {}); // 链不因单次失败断
  return p;
}

// 语音转写：返回 {text|null, reason}。reason 解释为什么没转出（给回执用，失败要响）。
async function transcribeVoice(fp, { mock = false } = {}) {
  if (mock) return { text: '这是一条测试语音的转写文本(mock)', reason: null };
  if (VOICE_ASR !== 'xfyun' || !ENV.XFYUN_APP_ID) return { text: null, reason: '未配 ASR' };
  if (!(await ffmpegOk())) return { text: null, reason: '没装 ffmpeg' };
  try {
    const text = await enqueueAsr(fp);
    return { text: text || null, reason: text ? null : '没识别出文字' };
  } catch (e) {
    console.error('[media][asr]', e.message);
    return { text: null, reason: '转写失败：' + e.message };
  }
}

// ---- media_log 落库（两层：file_path 不可逆 + transcript 可再生） ----
function storeMedia({ sessionId, senderId, kind, fp, transcript, model }) {
  return tx((c) => {
    const info = c.prepare(
      `INSERT INTO media_log (ts, session_id, sender_id, kind, file_path, transcript, model, coded_at)
       VALUES (?,?,?,?,?,?,?,?)`,
    ).run(nowMs(), sessionId ?? null, String(senderId ?? ''), kind, fp, transcript ?? null, model ?? null, model ? nowMs() : null);
    return Number(info.lastInsertRowid);
  });
}

// =====================================================================
// handleMedia —— 媒体总处理：下载 → 描述/转写 → media_log → (图)媒体 episode。
// 返回 { mediaLogId, receipt, feedText, desc }：
//   - receipt：要发回给子淇的【协议层】回执（仅失败告知："再发一次"/"没识别出"）；null=不发回执。
//     成功媒体不回执——对话层遵循人类微信节律，回应由模型在整轮回复里一次说清（边界原则，见 ARCHITECTURE.md）。
//   - feedText：语音转写出的文本（adapter 当主人打的字进轮次装配）；null=不喂。
//   - desc：图片的客观描述（adapter 进轮次装配，供模型判断图配哪句话）；null=无。
// 本模块不直接发消息（不 import adapter，避免环）；发送由 adapter 决定。
// =====================================================================
export async function handleMedia({ senderId, sessionId = null, msgData, kind, mock = false }) {
  const fp = await downloadMedia(msgData, kind, { mock });
  if (!fp) {
    return { mediaLogId: null, receipt: kind === 'image' ? '图片没收完整，再发一次？' : '语音没收完整，再发一次？', feedText: null, desc: null };
  }

  if (kind === 'image') {
    const desc = await describeImage(fp, { mock });
    const id = storeMedia({ sessionId, senderId, kind: 'image', fp, transcript: desc, model: desc ? VISION_MODEL : null });
    // 描述写普通 episode（role=user, entity=media）→ retrieveMedia 召回索引（防 tell #5）；近窗由装配 episode 承担。
    const body = desc ? `[media#${id}|图片] ${desc}` : `[media#${id}|图片] （图片已存，暂没识别出内容）`;
    safeEpisode({ sessionId, role: 'user', content: body, entity: 'media', taskId: id });
    if (desc) return { mediaLogId: id, receipt: null, feedText: null, desc }; // 成功：描述进装配层，不单独回执
    return { mediaLogId: id, receipt: '图片存下了 📷（暂没识别出内容）', feedText: null, desc: null };
  }

  // voice
  const { text, reason } = await transcribeVoice(fp, { mock });
  const id = storeMedia({ sessionId, senderId, kind: 'voice', fp, transcript: text, model: text ? (VOICE_ASR || 'mock') : null });
  if (text) {
    // 语音=打字等价：转写文本进轮次装配当主人打的字（装配层 flush 后 onMessage 落 user 轮 + 跑 agent）。
    // 不另写 media episode，避免与 onMessage 落的 user 轮重复；原始音频已在 media_log 兜底。
    return { mediaLogId: id, receipt: null, feedText: text, desc: null };
  }
  // 没转出文字：原始音频零丢失，给明确回执（失败要响，不静默）。
  return { mediaLogId: id, receipt: `语音存下了 🎙️（${reason || '没转成文字'}），原始音频已留存，回头能补转。`, feedText: null, desc: null };
}

// episodes 落库失败不拖垮媒体处理（失败要响但降级）。
function safeEpisode(args) {
  try { appendEpisode(args); } catch (e) { console.error('[media] appendEpisode 失败(忽略): %s', e.message); }
}

// =====================================================================
// --selftest：离线 mock（不联网、不真下载/转写），验证两层落库 + 媒体 episode + 回执/feedText 语义。
// 运行：node media.mjs --selftest
// =====================================================================
async function runSelftest() {
  const { mkdtempSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const db = await import('./db.mjs');

  let pass = 0, fail = 0;
  const ok = (c, m) => { if (c) { pass++; console.log('  ✓ ' + m); } else { fail++; console.log('  ✗ ' + m); } };

  const dir = mkdtempSync(join(tmpdir(), 'xw2-media-'));
  process.env.XW2_DB_PATH = join(dir, 'v2.db');
  process.env.MEDIA_DIR = join(dir, 'media');

  try {
    const conn = db.initDb(process.env.XW2_DB_PATH);

    // 图片：mock 下载+描述 → media_log 两层 + media episode + desc 交装配层（成功不回执，对话层边界）
    const img = await handleMedia({ senderId: 'OWNER', sessionId: 'wecom:OWNER', msgData: { fileId: 'x', fileAeskey: 'y' }, kind: 'image', mock: true });
    ok(img.mediaLogId > 0 && img.receipt === null && img.desc && img.feedText === null, '图片(识别成功)：返回 desc 供装配、不发独立回执');
    const mrow = conn.prepare('SELECT * FROM media_log WHERE id=?').get(img.mediaLogId);
    ok(mrow && mrow.kind === 'image' && mrow.file_path && mrow.transcript, 'media_log 两层落库（file_path + transcript）');
    const ep = conn.prepare(`SELECT * FROM episodes WHERE entity='media' AND task_id=?`).get(img.mediaLogId);
    ok(ep && ep.role === 'user' && ep.content.includes(`[media#${img.mediaLogId}|图片]`), '图片描述写 media episode（role=user, [media#N] 锚点）');

    // retrieveMedia 能召回这张图
    const mem = await import('./memory.mjs');
    const hits = mem.retrieveMedia('牛肉面', 2);
    ok(hits.length >= 1 && hits[0].content.includes('media#'), 'retrieveMedia 召回到媒体描述');

    // 语音：mock 转写 → media_log + feedText（不发回执，交 onMessage）
    const voc = await handleMedia({ senderId: 'OWNER', sessionId: 'wecom:OWNER', msgData: { fileId: 'v', fileAeskey: 'k' }, kind: 'voice', mock: true });
    ok(voc.mediaLogId > 0 && voc.feedText && voc.receipt === null, '语音(转写成功)：返回 feedText、不发回执（交 onMessage 走主链路）');
    const vrow = conn.prepare('SELECT * FROM media_log WHERE id=?').get(voc.mediaLogId);
    ok(vrow && vrow.kind === 'voice' && vrow.file_path && vrow.transcript, '语音 media_log 两层（音频 + 转写）');
    // 语音不另写 media episode（避免与 onMessage 的 user 轮重复）
    const vep = conn.prepare(`SELECT COUNT(*) c FROM episodes WHERE entity='media' AND task_id=?`).get(voc.mediaLogId);
    ok(vep.c === 0, '语音不另写 media episode（避免重复）');

    // 下载失败 → 回执提示重发
    const bad = await handleMedia({ senderId: 'OWNER', sessionId: 'wecom:OWNER', msgData: {}, kind: 'image', mock: false });
    ok(bad.mediaLogId === null && /再发一次/.test(bad.receipt), '下载失败：回执提示重发，不崩');

    // 大小卡口：声明超大的文件直接拒收（防 1.6G 机 OOM）
    const big = await handleMedia({ senderId: 'OWNER', sessionId: 'wecom:OWNER', msgData: { fileId: 'x', fileAeskey: 'y', fileSize: 99 * 1024 * 1024 }, kind: 'image', mock: false });
    ok(big.mediaLogId === null, '超大声明文件被拒收（防 OOM/事件循环阻塞）');

    // SSRF 兜底：path3 内网/元数据媒体 URL 被拦
    const ssrf = await handleMedia({ senderId: 'OWNER', sessionId: 'wecom:OWNER', msgData: { fileHttpUrl: 'http://169.254.169.254/latest/meta-data/' }, kind: 'image', mock: false });
    ok(ssrf.mediaLogId === null, '内网/元数据媒体 URL 被 SSRF 兜底拦截');
  } catch (e) {
    fail++;
    console.log('  ✗ selftest 异常: ' + e.stack);
  } finally {
    try { (await import('./db.mjs')).__closeForTest(); } catch {}
    rmSync(dir, { recursive: true, force: true });
  }

  console.log(`\n[media.mjs selftest] PASS ${pass} / FAIL ${fail}`);
  process.exit(fail ? 1 : 0);
}

if (process.argv.includes('--selftest') && import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  await runSelftest();
}
