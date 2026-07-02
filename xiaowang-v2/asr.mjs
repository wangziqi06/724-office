// asr.mjs — 讯飞「中英识别大模型」(spark_zh_iat) 语音转写（从 digital-twin 迁移）
// 协议: wss://iat.xf-yun.com/v1，HMAC-SHA256 鉴权，raw PCM 16k/mono/16bit，
//        分帧 status 0(首)/1(中)/2(尾)，dwa=wpgs 动态修正。
// 依赖: ffmpeg(外部进程，把语音转 PCM) + Node24 内置 WebSocket。silk-wasm 仅在遇 SILK 时【动态 import】，
//        所以本模块无 silk-wasm 也能 import（只在真要解 SILK 时才需装），符合 v2「能 import 即可自检」。
// 文档: xfyun.cn/doc/spark/spark_zh_iat.html

import { createHmac } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';

const HOST = 'iat.xf-yun.com';
const PATH = '/v1';
const FRAME = 1280;          // 每帧字节(文档建议 1280B/40ms)
const FRAME_INTERVAL_MS = 40;

// ffmpeg 通用：跑 ffmpeg，可选 stdin 喂 inputBuf，回收 stdout(Buffer)。
function ffmpegRun(args, inputBuf) {
  return new Promise((resolve, reject) => {
    const ff = spawn('ffmpeg', args);
    const out = [], err = [];
    ff.stdout.on('data', (d) => out.push(d));
    ff.stderr.on('data', (d) => err.push(d));
    ff.on('error', (e) => reject(new Error('ffmpeg spawn 失败(没装?): ' + e.message)));
    ff.on('close', (code) => {
      if (code !== 0) return reject(new Error('ffmpeg exit ' + code + ': ' + Buffer.concat(err).toString().slice(0, 200)));
      const pcm = Buffer.concat(out);
      pcm.length ? resolve(pcm) : reject(new Error('ffmpeg 产出空 PCM'));
    });
    if (inputBuf) { ff.stdin.on('error', () => {}); ff.stdin.write(inputBuf); ff.stdin.end(); }
  });
}

// 音频文件 → PCM 16k mono s16le(Buffer)。
// 企微/微信语音=SILK(ffmpeg 解不了)→ silk-wasm 解成 24k PCM 再 ffmpeg 重采样到 16k；其余(AMR/MP3/WAV)→ ffmpeg 直解。
// silk-wasm 只在确认是 SILK 文件时才动态 import：非 SILK / 没装 silk-wasm 也不影响 AMR 路径与模块加载。
async function toPcm(audioPath) {
  const raw = readFileSync(audioPath);
  const head = raw.slice(0, 12).toString('latin1'); // SILK 文件头含 'SILK'（'#!SILK_V3' 或带前导字节）
  if (head.includes('SILK')) {
    const { decode: silkDecode } = await import('silk-wasm'); // 仅 SILK 才需要，动态加载
    const { data } = await silkDecode(raw, 24000);            // 微信 SILK 标准 24k；data=pcm_s16le @24k
    return ffmpegRun(['-hide_banner', '-loglevel', 'error', '-f', 's16le', '-ar', '24000', '-ac', '1',
      '-i', 'pipe:0', '-ar', '16000', '-ac', '1', '-f', 's16le', '-'], Buffer.from(data));
  }
  return ffmpegRun(['-hide_banner', '-loglevel', 'error', '-i', audioPath, '-ar', '16000', '-ac', '1', '-f', 's16le', '-']);
}

// 讯飞标准鉴权：签名拼进 wss URL 的 authorization 参数。
function authUrl(apiKey, apiSecret) {
  const date = new Date().toUTCString(); // RFC1123 GMT
  const origin = `host: ${HOST}\ndate: ${date}\nGET ${PATH} HTTP/1.1`;
  const signature = createHmac('sha256', apiSecret).update(origin).digest('base64');
  const auth = Buffer.from(
    `api_key="${apiKey}", algorithm="hmac-sha256", headers="host date request-line", signature="${signature}"`
  ).toString('base64');
  return `wss://${HOST}${PATH}?` + new URLSearchParams({ authorization: auth, date, host: HOST });
}

// 主入口：音频文件 → 文本。失败抛错(调用方 media.mjs 兜成"只存音频")。
export async function transcribeVoice(audioPath, { appId, apiKey, apiSecret }) {
  const pcm = await toPcm(audioPath);
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(authUrl(apiKey, apiSecret));
    const segs = {};            // sn -> text，支持 wpgs(apd 追加 / rpl 替换)
    let settled = false, timer;
    const finalText = () => Object.keys(segs).map(Number).sort((a, b) => a - b).map((k) => segs[k]).join('').trim();
    const done = (err, text) => {
      if (settled) return; settled = true; clearTimeout(timer);
      try { ws.close(); } catch {}
      err ? reject(err) : resolve(text);
    };
    timer = setTimeout(() => done(new Error('asr 超时(75s)')), 75000);

    ws.addEventListener('open', async () => {
      try {
        let seq = 0;
        for (let off = 0; off < pcm.length; off += FRAME) {
          const chunk = pcm.subarray(off, Math.min(off + FRAME, pcm.length));
          const status = off === 0 ? 0 : 1;
          const frame = {
            header: { app_id: appId, status },
            payload: { audio: { encoding: 'raw', sample_rate: 16000, channels: 1, bit_depth: 16, seq: ++seq, status, audio: chunk.toString('base64') } },
          };
          if (status === 0) frame.parameter = { iat: { domain: 'slm', language: 'zh_cn', accent: 'mandarin', eos: 6000, dwa: 'wpgs', result: { encoding: 'utf8', compress: 'raw', format: 'json' } } };
          ws.send(JSON.stringify(frame));
          if (off + FRAME < pcm.length) await new Promise((r) => setTimeout(r, FRAME_INTERVAL_MS));
        }
        ws.send(JSON.stringify({ header: { app_id: appId, status: 2 }, payload: { audio: { encoding: 'raw', sample_rate: 16000, channels: 1, bit_depth: 16, seq: seq + 1, status: 2, audio: '' } } }));
      } catch (e) { done(new Error('发帧失败: ' + e.message)); }
    });

    ws.addEventListener('message', (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.header?.code !== 0) return done(new Error(`xfyun code=${msg.header?.code} ${msg.header?.message || ''}`));
      const t = msg.payload?.result?.text;
      if (t) {
        try {
          const r = JSON.parse(Buffer.from(t, 'base64').toString('utf8'));
          const text = (r.ws || []).flatMap((x) => (x.cw || []).map((c) => c.w)).join('');
          if (r.pgs === 'rpl' && Array.isArray(r.rg)) for (let i = r.rg[0]; i <= r.rg[1]; i++) delete segs[i];
          segs[r.sn] = text;
        } catch {}
      }
      if (msg.header?.status === 2) done(null, finalText());
    });
    ws.addEventListener('error', (e) => done(new Error('ws error: ' + (e?.message || 'unknown'))));
    ws.addEventListener('close', () => { if (!settled) done(null, finalText()); });
  });
}
