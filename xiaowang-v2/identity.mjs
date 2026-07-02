// =====================================================================
// identity.mjs —— 实例身份（主人是谁 / 个性化批注 / 默认城市）从代码抽出，按实例注入。
//
// 一套代码多实例（路2 隔离的"参数化身份"基石）：每个实例用环境变量 XW2_IDENTITY 指向
// 一份身份配置（identities/<name>.json，或绝对路径）。默认 = ziqi（向后兼容：子淇现有实例
// 不设这个 env 也能照常加载到自己的身份，行为零变化）。
//
// 隔离靠结构：朋友实例只加载朋友的身份 + 朋友自己的库，共享代码里【不再写死任何人的名字/城市】。
// 谁的个人信息都不在共享代码里，只在各自的 identity 文件 + 各自的库里。
//
// 依赖方向：本模块零业务依赖（只 fs 读 JSON + 纯默认），谁都能 import，可整块删除（原则6）。
// =====================================================================

import { readFileSync, existsSync } from 'node:fs';
import { join, isAbsolute } from 'node:path';

const DIR = import.meta.dirname;

// 安全默认：身份文件缺失/损坏时不崩，用一个无任何人个人信息的中性身份兜底（失败要响但不致命）。
const DEFAULT_IDENTITY = {
  ownerName: '主人',           // 小王对主人的称呼
  profileNote: '',             // 主人专属的行为批注（如漂移提醒）；通用版留空
  weatherMorningCities: ['上海'],   // 早播天气城市
  weatherEveningCities: ['上海'],   // 晚播天气城市
};

function resolvePath() {
  const v = process.env.XW2_IDENTITY;
  if (!v) return join(DIR, 'identities', 'ziqi.json'); // 默认 = 子淇（向后兼容）
  if (isAbsolute(v) || v.includes('/') || v.includes('\\')) return v; // 显式路径
  return join(DIR, 'identities', `${v}.json`); // 简写：'friend' → identities/friend.json
}

function load() {
  const p = resolvePath();
  try {
    if (existsSync(p)) {
      const obj = JSON.parse(readFileSync(p, 'utf8'));
      // 合并默认：身份文件没写的字段用默认兜底（如朋友没配城市）
      return { ...DEFAULT_IDENTITY, ...obj };
    }
    console.error('[identity] 身份文件不存在(%s)，用中性默认身份', p);
  } catch (e) {
    console.error('[identity] 身份文件加载失败(%s)，用中性默认身份: %s', p, e.message);
  }
  return { ...DEFAULT_IDENTITY };
}

// 启动时解析一次（身份在进程生命周期内不变；要换身份=换 env 重启）。
export const IDENTITY = load();
export const OWNER_NAME = IDENTITY.ownerName;
