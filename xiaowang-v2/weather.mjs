// =====================================================================
// weather.mjs —— 高德天气播报（确定性，不经 LLM）。
//
// 为什么不让 agent 临场查：天气是固定动作（取数→格式化→发），不需要模型判断；
// 走 LLM 反而不稳、还会夹带"记得带伞哦~"这类子淇极反感的 AI 味旁白（原则10）。
// 这里直接打高德天气端点，按固定陈述式格式拼文案，零模型参与、可复现、便宜。
//
// KEY/能力恢复自旧小王（patch_capabilities.py）。优先读 .env 的 GAODE_KEY，留空则天气功能不可用，需自行申请。
// 依赖方向：本模块零业务依赖（只 fetch + 纯函数格式化），可整块删除（原则6）。
// =====================================================================

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { IDENTITY } from './identity.mjs';

const DIR = import.meta.dirname;

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

// 高德 web 服务 key：从 .env 的 GAODE_KEY 读取；留空则天气功能不可用。
const GAODE_KEY = ENV.GAODE_KEY || '';
const WEATHER_URL = 'https://restapi.amap.com/v3/weather/weatherInfo';
const HTTP_TIMEOUT_MS = parseInt(ENV.HTTP_TIMEOUT_MS || '15000', 10);

// 城市 → adcode（高德标准行政编码）。子淇相关：上海/深圳；北京备用。
export const ADCODE = { 上海: '310000', 深圳: '440300', 北京: '110000' };

// ---- 带 timeout 的高德调用（致命纪律②） ----
async function fetchForecast(adcode) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(new Error('timeout')), HTTP_TIMEOUT_MS);
  try {
    const url = `${WEATHER_URL}?key=${GAODE_KEY}&city=${adcode}&extensions=all`;
    const r = await fetch(url, { signal: ac.signal });
    const j = await r.json();
    if (j.status !== '1' || !Array.isArray(j.forecasts) || !j.forecasts[0]) {
      throw new Error(`高德返回异常: status=${j.status} info=${j.info} infocode=${j.infocode}`);
    }
    return j.forecasts[0]; // { city, casts:[{date,dayweather,nightweather,daytemp,nighttemp,...}] }
  } finally {
    clearTimeout(timer);
  }
}

// ---- 纯函数格式化（便于 selftest 用 fixture 验，不联网） ----
const WEEK_CN = ['日', '一', '二', '三', '四', '五', '六'];
function dowOf(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  return WEEK_CN[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}
const hasRain = (c) => /雨|雪|雷/.test(`${c.dayweather}${c.nightweather}`);

// 一天一行：标签 天气 低~高°。今天/明天用相对词，其余用"周X"。
function fmtDay(cast, idx) {
  const label = idx === 0 ? '今天' : idx === 1 ? '明天' : `周${dowOf(cast.date)}`;
  const wx = cast.dayweather === cast.nightweather ? cast.dayweather : `${cast.dayweather}转${cast.nightweather}`;
  return `${label} ${wx} ${cast.nighttemp}~${cast.daytemp}°`;
}

/**
 * 把一份 forecast 渲染成一个城市块。
 * @param {{city:string,casts:Array}} forecast
 * @param {number} fromIdx 从第几天起（0=含今天=早播；1=从明天=晚播未来几天）
 * @param {number} days 取几天
 */
export function renderCity(forecast, fromIdx = 0, days = 4) {
  const casts = forecast.casts.slice(fromIdx, fromIdx + days);
  const lines = casts.map((c, i) => fmtDay(c, fromIdx + i));
  const rainy = casts.filter((c) => hasRain(c));
  const head = `${forecast.city.replace('市', '')}天气`;
  let out = `${head}\n${lines.join('\n')}`;
  if (rainy.length) {
    const when = rainy.map((c, i) => fmtDay(c, fromIdx + casts.indexOf(c)).split(' ')[0]).join('、');
    out += `\n🌂 ${when}有雨，带伞`;
  }
  return out;
}

// ---- 两个预设：早播(单城含今天) / 晚播(多城未来几天) ----
// 失败要响：取数失败给出明确文案，不静默吞、不发空（原则"失败要响"）。
export async function weatherBriefing({ cities = ['上海'], fromIdx = 0, days = 4 } = {}) {
  const blocks = [];
  for (const name of cities) {
    const adcode = ADCODE[name] || name; // 允许直接传 adcode
    try {
      const f = await fetchForecast(adcode);
      blocks.push(renderCity(f, fromIdx, days));
    } catch (e) {
      console.error('[weather] %s 取数失败: %s', name, e.message);
      blocks.push(`${name}天气：暂时取不到（${e.message}）`);
    }
  }
  return blocks.join('\n\n');
}

// 早 8:30 主人的早播城市（含今天 + 未来 3 天）。城市来自 identity（子淇=上海，朋友=各自配）。
export const morningBriefing = () => weatherBriefing({ cities: IDENTITY.weatherMorningCities, fromIdx: 0, days: 4 });
// 晚 22:30 主人的晚播城市（未来几天，从明天起）。
export const eveningBriefing = () => weatherBriefing({ cities: IDENTITY.weatherEveningCities, fromIdx: 1, days: 3 });

// =====================================================================
// --selftest：纯函数格式化用 fixture 验（不联网）；带 --live 才真打高德。
// =====================================================================
const FIXTURE = {
  city: '上海市',
  casts: [
    { date: '2026-06-25', dayweather: '多云', nightweather: '阴', daytemp: '26', nighttemp: '21' },
    { date: '2026-06-26', dayweather: '多云', nightweather: '阴', daytemp: '29', nighttemp: '21' },
    { date: '2026-06-27', dayweather: '小雨', nightweather: '阴', daytemp: '28', nighttemp: '22' },
    { date: '2026-06-28', dayweather: '阴', nightweather: '阴', daytemp: '29', nighttemp: '24' },
  ],
};

if (process.argv.includes('--selftest') && import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  let pass = 0, fail = 0;
  const ok = (c, m) => { console.log(`  ${c ? '✓' : '✗'} ${m}`); c ? pass++ : fail++; };
  console.log('weather.mjs selftest (fixture, 不联网)\n');

  const r = renderCity(FIXTURE, 0, 4);
  ok(/^上海天气/.test(r), '城市块标题=上海天气');
  ok(r.includes('今天 多云转阴 21~26°'), '今天行：相对词+天气+温度范围');
  ok(r.includes('明天 多云转阴 21~29°'), '明天行');
  ok(/周六 小雨转阴 22~28°/.test(r), '第三天用"周X"标签');
  ok(/🌂.*有雨，带伞/.test(r), '含雨天 → 带伞提示');
  ok(!/记得|哦~|呀|贴心|注意保暖/.test(r), '无 AI 味旁白（原则10）');

  const noRain = renderCity({ city: '深圳市', casts: FIXTURE.casts.slice(3) }, 0, 1);
  ok(!/🌂/.test(noRain), '无雨天 → 不加带伞行');
  ok(/^深圳天气/.test(noRain), '城市名去"市"');

  // --live：真打高德验证 key + 链路
  if (process.argv.includes('--live')) {
    (async () => {
      const m = await morningBriefing();
      ok(/上海天气/.test(m) && /°/.test(m), `[live] 早播取到上海真实天气:\n${m}`);
      console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
      process.exit(fail ? 1 : 0);
    })();
  } else {
    console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
    process.exit(fail ? 1 : 0);
  }
}
