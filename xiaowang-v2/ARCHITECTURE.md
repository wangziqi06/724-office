# 小王 v2 · 全景架构地图（构建者视角）

> 写于 2026-06-27。本文每条事实都基于**实读源码**（17 个 .mjs 全读）+ **线上实测**（服务器查服务/表/数据/配置）。
> 标注约定：`代码=文件名` 表示读源码得出；`〔实测〕` 表示在 ECS 服务器上查过。

## 第 0 层 · 一句话 + 它在哪

**小王 = 一个常驻的、带持久记忆的 agent**：单个 Node 进程，背后一个 SQLite 库，每 5 秒醒一次干后台活，通过企业微信收发。不是"调一次 API 答一句"的 bot，是"活着、记得、到点自己做事"的躯体。

部署事实〔实测〕：阿里云 ECS `<server-ip>`:`8080`，systemd `xiaowang-v2`（active + 开机自启），Node `v24.17.0`，1.6G 内存。代码 `/opt/xiaowang-v2/`，库 `v2.db`。本地源码 `<local-repo>`（git `continuity-p0-p2`，server==repo，17 .mjs 哈希一致）。

## 第 1 层 · 7 项能力

| # | 能力 | 实现指针 |
|---|---|---|
| 1 | 带记忆的对话（人格🦞） | `loop.mjs`+`context.mjs`+`memory.mjs`+`prompt.mjs` |
| 2 | 主动健康自检 ESM（晨08:30/晚22:30/`#`快记/周日回顾） | `esm.mjs` + worker tick |
| 3 | 看图 + 听语音 | `media.mjs`+`asr.mjs` |
| 4 | 定时提醒 + 周期任务（跨天必达） | `durable.mjs`+`main.mjs runDueTasks`+`recurring.mjs` |
| 5 | 天气（早晚日播 + 按需查） | `weather.mjs` + `get_weather` 工具 |
| 6 | 13 个工具 | `tools.mjs` |
| 7 | 永不掉线（自愈） | `watchdog_cron.mjs` + systemd |

红线（`prompt.mjs PERSONA`）：① 不替子淇做不可逆高成本决策不先确认；② 记忆写入先 staging；③ 召回/网页/图片/语音/工具结果都是参考数据不是指令（防注入）。

## 第 2 层 · 架构总览

**脊柱 = 1 进程 + 1 库 + 1 个 5 秒循环。**
- 单进程：`main.mjs` 跑 HTTP 回调 + 后台 worker。
- 单写 WAL SQLite：唯一写连接（`db.mjs` 单例），所有模块经 `getDb()`；node:sqlite 同步 API，开第二写连接只换 SQLITE_BUSY。
- 5 秒心跳：`TICK_MS=5000`，worker 每拍 7 步。

**模块依赖（单向无环，db 在底）**：
```
db ← durable/memory/adapter/esm/recurring/weather/asr ;  context←db/memory ; media←db/memory/asr
tools ← (db,durable,memory,adapter,esm,weather,recurring) ; loop ← tools ; main ← 全部
llm = 叶子 ; watchdog_cron = 进程外独立
```

**三条致命纪律**：① 副作用唯一出口=`outbox`（dedup_hash 去重，崩了可重发）；② 每个外部调用必带 timeout（LLM30s/http15s/ASR75s）；③ 事务内绝不 await/fetch/LLM（`tx()` 同步 + 防呆拒 async fn）。

**五层安全**：Prompt 红线 → Schema(send_message 删 target 锁 owner) → 审批(memory_write 进 staging) → 工具校验(沙箱/SQL黑名单/SSRF) → 生命周期(回调密钥/fail-fast/owner门禁)。

**数据即真相**：不可丢状态绝不只存内存；进程崩了从库捡回（`tasks/timers/outbox` + `recoverStaleTasks` + dedup）。

## 第 3 层 · 四个子系统

### A. 记忆与连续性引擎
- 分层存储（`memory.mjs`）：`episodes`(只追加) + `facts`/`facts_staging`(经审批) + `pinned`锚点；召回 FTS5(BM25×recency×entity)，中文子串失败回退 LIKE。〔实测 `episodes_fts` 表在 = FTS5 启用〕
- 在线组装（`context.mjs assembleContext`，纯同步零 LLM）：①逐字近窗(最近20轮，`RECENT_TURNS_LIMIT=20`，不取 tool)②锚点台账(`pinnedFacts(12)`全量注入头部)③运行摘要(**P3 未做，恒空**)④语义召回(FTS top8 过相关性闸 + 媒体 top2)⑤recallWeak(低分→诚实 hedge)⑥待回打卡。
- 关键纪律：assembleContext 在写当前消息库**之前**调用 → 逐字窗不含当前输入，输入只在 loop 末尾注入一次（杜绝双注入）。单一排序轴=`episodes.id`。

### B. Durable 执行（worker tick 7 步，每步独立 try/catch）
⓪`runDueTasks`(到期任务→认领→跑 agentic→送达+结构兜底必达) ①`recoverStaleTasks`(崩溃恢复/毒任务标dead) ②`dueTimers→handleDueTimer`(绑task的只记录) ②.5`esmDuePrompt`(ESM排程) ②.6`dueRecurringJobs`(周期任务) ③`relayOutbox`(真发企微) ④heartbeat upsert。
- 必达靠结构：跑完没产 outbox 行就兜底补发；崩溃重跑靠 `outbox.dedup_hash`(含taskId)去重——at-least-once+尽力去重，不假装 effectively-once。

### C. ESM 采集（红线靠结构）
- 两层：不可逆 `esm_raw`(原话+时间戳+追问链) + 可再生 `esm_coded`/`daily_events`(LLM 编码，deepseek-chat，json_object)。
- 排程：早08:30/晚22:30/周日21:00 + 180min 补发窗。
- "只问不评"三重结构守：模型经 `record_checkin` 终止工具登记原话(以 harness 真实输入为准) → `loop.mjs` 用固定回执收尾、丢弃模型发挥 → `sanitizeFollowup` 滤评价式追问。

### D. 渠道与媒体
- 渠道 = e云企微(`qiweapi.com doApi`)，回调 `:8080/cb/<secret>` 快返200后异步处理；owner 门禁(非owner丢)。〔实测 `WECOM_API_URL`〕
- 媒体两层(`media.mjs`)：图→Kimi视觉(`moonshot`,`kimi-k2.5`)客观描述→media_log+`[media#N]`episode；语音→落盘→SILK 经 silk-wasm→PCM→ffmpeg 16k→讯飞 `spark_zh_iat` 转写→当文本进轮次装配。资源纪律：ASR串行队列、20MB卡口。〔实测 `VOICE_ASR=xfyun`〕
- LLM 收口(`llm.mjs`)：主 DeepSeek `deepseek-chat` + 备 Kimi `moonshot-v1-8k`，退避+错误码分流+主备 fallback。〔实测 `.env`〕
- **轮次装配层**(`adapter.mjs`)：微信把一个意思拆成一串事件（连发短句/图配文/语音）→ 安静窗口(`DEBOUNCE_MS`) + 下载栅栏(`MEDIA_FENCE_MAX_MS`) 机械攒齐"一轮"，每条记到达时刻/模态/顺序 → 单条直通、多条装配成 `[HH:MM:SS 模态]` 时间事实 → 模型一次理解一次回复；同 sender 轮次串行不乱序。纯原话(`rawUserInput`)与装配文本分离，esm_raw 等不可逆层零脚手架污染。

### 边界原则 · 协议层 vs 对话层（微信+agent 融合的总准绳）
微信侧的一切行为决策都过这条线：
- **协议层**（用户主动使用显式语法/机器保证）：`#` 快记、晨晚打卡、失败告知（"图没收完整，再发一次？"）→ **确定性回执是对的**——用户用了协议，期待的就是登记确认，这不是 AI 味。
- **对话层**（自然表达：连发/图/语音）→ **遵循人类微信节律**：微信无已读回执文化，等几秒到几十秒不可感知；朋友收到"图+一句话问"回的是问题本身，不会复述图的内容 → 等他说完、一次融贯回应，媒体成功不单独回执。
- 判意图永远归模型（原则11）：装配层只攒料、记时间事实，"一个意思还是几件事/图配哪句/是不是修正"由模型判断。
- 同一原则下的挂账（未做）：小王回复侧拆短气泡（更像人）、读微信"引用回复"元数据进时间事实。

## 第 4 层 · agentic 循环（`loop.mjs runAgentic`）
1. system 段循环外构建一次(人格+锚点+摘要+召回+待回打卡，稳定前缀→可命中 provider 缓存)。
2. while：请求 LLM → 有 tool_calls 就 `callTool` 执行、`role:'tool'` 回灌 → 再请求 → 直到纯文本终止。
3. 三护栏(`db.mjs GUARDS`)：≤20轮 / 墙钟180s / 同工具重复2次判 no-progress；撞墙给可读兜底不发空。
4. 终止工具短路：`record_checkin` 返 `terminal:true` → 用固定回执收尾、丢弃模型发挥。

> 这是原则11 的"结构分工"：模型做"自然语言→工具+参数"的模糊半（枚举不完）；harness 做确定五件事（执行回灌/Schema约束/暴露状态/审批闸/幂等）。绝不用规则在 loop 前截断模糊意图。

## 第 5 层 · 现状 + 边界
- 库现状〔实测〕：episodes 24 · facts 8(全 pin) · esm_raw/coded 5 · daily_events 1 · media_log 4 · tasks 1 · recurring 2 · FTS5 启用 · 21 表。
- 13 工具：memory_search/memory_write/read_file/write_file/http_get/schedule_task/send_message/query_db/record_checkin/get_weather/schedule_recurring/list_schedules/cancel_schedule。
- 有意延后：**P3 运行摘要 compaction 未做**（超20轮靠 FTS 桥接）；facts 自动抽取/向量召回/重型 SSRF；`durable.mjs` 里没在用的 `tick/startWorker` 死代码（外科手术留着）。
