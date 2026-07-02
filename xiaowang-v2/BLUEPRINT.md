## 小王 v2 架构蓝图 —「磐石核 · 收口版」(Lobster Core, red-team-hardened)

### 0. 一句话
一个 Node 进程 + 一个 WAL SQLite 文件 + 一个手写 while 循环。四样超能力都从同一个「持久执行内核」长出来。**首版砍到能在 ECS 上跑通一整夜的最小可靠核(≤4 文件 / ≤6 表)**,frontier 件全部进 backlog,跑稳了再按真实痛点逐个加。

### 1. 这份蓝图怎么来的(合成决策,先交代)
- **骨架选 设计1 Lobster Core**:模块边界最干净、durable 内核最严谨、"可靠性来自结构不来自聪明"的哲学和硬约束最贴。
- **嫁接 设计3**:FTS5 不当既定事实、备 LIKE 降级(设计3 是三套里唯一对 FTS5 诚实的);episodes 表加 `entity` 列(便于实体 boost 召回)。
- **嫁接 设计2**:失败教训落 `attempt_notes` 的思路保留为可选 backlog,但 **砍掉 Ralph Loop 全套自主重注入**(对 n=1 是 cargo cult)。
- **逐条消化两个红队**:见 §2,被指出会炸/过度的要么改要么砍,不嘴硬。

### 2. 红队意见逐条处置(这是这份蓝图区别于三套原稿的核心)

| 红队指控 | 真伪判定 | 处置 |
|---|---|---|
| **FTS5 在 node:sqlite 不存在,建表即崩** | **我已实测:本地 Node 22 上 `CREATE VIRTUAL TABLE USING fts5` + WAL + synchronous=FULL 全 OK**。红队1 的"编译期没带 FTS5"在当前环境不成立。但**ECS 必须复测**(live bot 已用 node:sqlite+WAL 在 ECS 跑,但没用过 FTS5) | **不靠"记得写探针",做成建表时 try/catch 自动分支**:`db.mjs` 初始化时 try 建 fts5 虚表,catch 落到 LIKE schema,运行时一个 `MEMORY_MODE='fts5'|'like'` 标志位决定召回 SQL。降级是代码,不是流程。 |
| **idempotency_key = taskId+stepSeq 在 LLM 非确定性重放下错位** | **真 bug,三套通病,最该修** | **幂等键不用 step 序号,用副作用内容的确定性 hash**:`send_message` 用 `sha256(channel+target+正文)`;LLM 调用用 `sha256(model+messages)`。step_seq 只用于 memo 跳过(读路径),去重键用内容 hash(写路径)。两者解耦。 |
| **DatabaseSync 同步阻塞 × 单进程 × WatchdogSec=30s = 长任务被误杀/自杀循环** | **真,且物理矛盾** | **首版直接不配 WatchdogSec**(live bot 现在就只有 `Restart=always RestartSec=5`,没 watchdog,跑得好好的)。进程级只做 crash-restart。"卡死检测"降级为:外部 cron 每 5min 查 heartbeat 表时间戳,静默 >10min 才 `systemctl restart` + 报警。hang 检测灵敏度换稳定,值。 |
| **同步 fsync 卡 event loop / "WAL 让读写并发"在单同步连接上是 0 收益的死概念** | **真** | **承认全串行**(n=1 负载低,可行)。纪律:① DB 写做小事务,单条 UPDATE 原子改 status+result;② **LLM/HTTP 调用走 async fetch,不在 DB 事务里、不占任何锁**;③ 不开第二写连接(避免 SQLITE_BUSY)。蓝图里删掉所有"WAL 让读写并发互不阻塞"的错误表述——WAL 在这里的真实价值只是 crash 安全 + 读不脏读,不是并发。 |
| **effectively-once 是假的**(SQLite 事务无法和外部 HTTP 原子提交) | **真,诚实性缺陷** | **改口径:at-least-once + 尽力去重,极小概率重发**。不再宣称 effectively-once。`send_message` 顺序固定为"先查 dedup → 发 → 立刻写 dedup",崩在中间窗口会重发一次——n=1 一天几条消息,可接受;且企微侧重发同文案不致命。 |
| **over-engineering:8~12 模块/11 表,违背自己喊的"少即是多"** | **真——确属 over-engineering，砍** | **硬护栏:首版文件 ≤7(实际 6)、表 ≤6**。砍掉:三态熔断→降级为重试2次+Kimi fallback;dead_letter→首版用 tasks.status='dead' 一个字段;Sawtooth/Facts-supersede 自动链/每轮自动抽取/向量列/spawn_subagent/OTel 字段/Ralph attempt_notes——**全进 backlog,首版一个不进**。 |
| **每轮跑小 LLM 抽取 facts = 自动放大 MEMORY.md 旧病(抽错的"事实"污染上下文)** | **真** | **首版:episodes 全留(只追加,无损);facts 不自动抽取**。检索就召回最近+关键词命中的 episodes。facts 表建好但**首版只手动/半自动写入**(经一个 `staging` 审核位),跑稳几周再考虑开自动抽取。 |
| **missed-timer 补跑语义欠定义**(关机3天,每天22:30 的 timer 补1次还是3次) | **真,核心用例欠定义** | **明确三态 catchup_policy**:`'skip'`(只跑下一次,如每日提醒)、`'once'`(过期补跑一次,如"3天后提醒")、`'all'`(每个错过的都补,罕见,默认不给)。周期 timer 默认 `'skip'`。 |
| **报警依赖进程内 outbox relay,进程彻底死时报警发不出** | **真,报警链在最该报警时断裂** | **报警走进程外**:一个独立的极小 cron 脚本 `watchdog_cron.mjs`(systemd timer 或 crontab,**不依赖主进程**)查 heartbeat 时间戳,超时直接调企微发送 API 报警。主进程内的 outbox 只管业务消息。 |
| **better-sqlite3 降级与"零依赖"哲学矛盾 / sqlite-vec 需 loadExtension+ABI 匹配** | **真** | **首版不引 sqlite-vec、不引 better-sqlite3**。FTS5 实测可用,降级目标是 LIKE(纯内置),不是 native 模块。向量整块进 backlog。 |
| **方案空间没发散:能不能 durable 不做 step 级,只做任务级整体重跑?** | **红队2 的最佳建议** | **采纳分级**:**确定性任务(定时器触发的固定动作)走 step 级 memo durable**;**agentic LLM 多步任务走"整体重跑 + 发送类副作用按内容 hash 去重"**这种更保守但安全的语义。不假装 LLM 多步任务能精确断点续。这是对"durable replay 套在非确定性 LLM 上不安全"的正面回应。 |

### 3. 总体结构(文字图)

```
                  ┌─────────────────────────────────────────┐
   inbound        │            main.mjs (单进程)              │
  ┌────────┐      │                                          │
  │ CLI    │─────▶│  adapter → ┌──────────────────────────┐  │
  │ /企微   │      │            │  loop.mjs  agentic while  │  │
  └────────┘      │            │  组上下文→调LLM→执行工具   │  │
                  │            │  →回灌→护栏判终止          │  │
                  │            └──────────┬───────────────┘  │
                  │       ┌───────────────┼──────────────┐   │
                  │       ▼               ▼              ▼   │
                  │  tools.mjs       memory.mjs      durable │
                  │ (注册表+幂等)    (episodes/facts)  .mjs   │
                  │       │               │          (tasks/ │
                  │       └──────┬────────┴────steps/timers/ │
                  │              ▼                  outbox)   │
                  │         db.mjs (一个 WAL .db, 全串行写)    │
                  │              ▲                            │
                  │     worker tick (每5s: 扫timer/续跑/      │
                  │      relay outbox/写heartbeat)            │
                  └──────────────┬───────────────────────────┘
                                 │ heartbeat 表
              ┌──────────────────▼─────────────────┐
              │ watchdog_cron.mjs (进程外, crontab) │
              │ 查心跳超时 → systemctl restart+报警  │
              └────────────────────────────────────┘
```

本体只有 `db.mjs + loop.mjs + worker(在 main 里) + tools/memory/durable`。其余皆可删。

### 4. 四样超能力落地(到表/机制级)

#### 4.1 durable(跨天稳定执行)
**表(全在一个 v2.db, WAL + synchronous=FULL + busy_timeout=5000):**
- `tasks(id PK, kind, status['pending'|'running'|'done'|'failed'|'dead'], next_run_at INT, idempotency_key UNIQUE, payload JSON, attempts INT, created_at, updated_at, heartbeat_at)`
- `steps(task_id, step_seq, status['pending'|'complete'], tool_name, params BLOB, return_value BLOB, attempts, ts, PRIMARY KEY(task_id, step_seq))`
- `timers(id PK, fire_at INT, task_id, payload JSON, catchup_policy['skip'|'once'], status)`
- `outbox(id PK, channel, target, content, dedup_hash UNIQUE, status['pending'|'sent'], attempts, created_at, sent_at)`

**机制:**
- 派任务 = 写 `tasks` 行(业务行 + 任务行同一事务原子提交)。"3天后提醒做去留决策" = 一行 `timers.fire_at = now + 3d, catchup_policy='once'`。
- **分级 durable**(消化红队):
  - **确定性任务**(timer 触发的固定动作,如发提醒):走 step 级 memo,重放时 COMPLETE 的 step 直接返存值跳过。安全,因为步骤是确定的。
  - **agentic LLM 任务**:**不做 step 级精确断点续**。崩溃后**整体重跑**该 task,但所有发送类副作用经 `outbox.dedup_hash`(内容 hash) UNIQUE 约束去重——重跑时同内容消息插不进 outbox = 不重发。这是对"LLM 重放非确定性"的正面让步。
- 崩溃恢复:worker 重启扫 `status in('pending','running')` 且 `heartbeat_at` 陈旧的 task,按上述分级重放/重跑。**内存里绝不保任何不能丢的状态**,一切从 SQLite 读。
- 调度:worker `setInterval` 每 5s 一个 tick,扫 `timers.fire_at<=now` + 扫陈旧 task。无 cron 守护、无外部 scheduler。

#### 4.2 自愈
- **进程级**:systemd `Restart=always RestartSec=5`(**首版不配 WatchdogSec**,沿用 live bot 已验证配置)。
- **进程外看门狗**:`watchdog_cron.mjs`(crontab 每5min),查 `heartbeat` 表 ts,静默 >10min → `systemctl restart xiaowang-v2` + 直接调企微 API 报警(不经主进程 outbox,因为主进程可能已死)。
- **重试**:外部调用收口到 `callWithResilience()`:指数退避 1→2→4s + 30% jitter,封顶 2 次(学 Stripe);按错误分流(429/5xx 退避重试,4xx 不重试)。**不做三态熔断**(n=1 低频调用攒不满"连续5次")。失败 fallback:DeepSeek↔Kimi 主备切换,再不行诚实失败 + 报警。
- **原子写**:所有状态变更走 `db.tx()` 事务;单条 UPDATE 同时改 status+result。写文件产物走"临时文件→fsync→rename"。
- **agentic 内护栏**:maxTurns≤20 + wall-clock≤180s + no-progress 检测(相同 tool+params hash 连续2次)。**注意:180s < 任何外部 watchdog 阈值,且无 systemd watchdog,不存在长任务被误杀**。
- **毒任务**:attempts 超限 → `tasks.status='dead'`(首版不单独建 dead_letter 表),不阻塞队列。

#### 4.3 分层记忆
- `episodes(id PK, ts INT, session_id, role, content, entity, task_id)` + `episodes_fts`(FTS5,**建表 try/catch,失败则不建、召回走 LIKE**)
- `facts(id PK, entity, fact, source['user_said'|'inferred'|'external'], confidence, created_at, valid_from, superseded_by, importance, last_accessed, access_count)` —— **表建好,但首版只半自动写入**(经 staging 审核),不每轮自动抽取。
- **working** 不落库(当次拼的上下文)。
- **检索** `retrieve(query, k)`:
  - FTS5 可用:`episodes_fts` BM25 分 × recency 衰减(`exp(-Δdays/τ)`) + entity 命中 boost,top-k。
  - FTS5 不可用(降级):`content LIKE '%kw%'` + entity 精确 + `ORDER BY ts DESC LIMIT k`。n=1 几千条全表扫毫秒级,牺牲相关性排序,可接受。
- **注入永远 top-k,绝不整份塞**(对抗 context rot)。
- **遗忘**(首版极简):`episodes` 超 90 天的归档表(可回查);facts 被 supersede 的标记不删(审计链)。**Sawtooth 主动压缩进 backlog**。

#### 4.4 真·agentic 工具循环
手写 manual loop(~150 行,纯 fetch,不引 LangChain/Vercel SDK):
1. 工具注册表:`[{name, description, paramSchema, fn, sideEffect:bool}]`,渲染成 OpenAI 兼容 `tools` 字段。
2. while:组 messages(system+🦞人格 + `retrieve()`召回 + history + tools) → `callWithResilience(POST /chat/completions)` → 解析 `tool_calls`。
3. 执行+回灌:每个 tool_call → 查注册表 → **有副作用的经统一 wrapper(派 dedup_hash + 危险操作正则黑名单)** → 执行 → 结果作 `role:'tool'` message 回灌。
4. 终止(loop 启动前硬编码):无 tool_calls / maxTurns / wall-clock / no-progress / 不可恢复错误。
5. **工具集首版收紧到 ≤8**:`memory_search`、`memory_write`(进 staging)、`read_file`/`write_file`(限沙箱目录)、`http_get`、`schedule_task`、`send_message`(走 outbox)、`query_db`(**只读、参数化、不暴露任意 SQL**——红队2 正确指出正则黑名单是纸糊防御,真防护是 schema 层不给任意 SQL 工具)。

### 5. 数据模型汇总(首版 6 表 + 2 FTS 虚表)
`v2.db`(WAL + synchronous=FULL):`tasks` / `steps` / `timers` / `outbox` / `episodes`(+`episodes_fts` 可选) / `facts`(+`facts_fts` 可选) / `heartbeat(id PK CHECK(id=1), ts, progress)`。
索引:`tasks(status,next_run_at)`、`timers(status,fire_at)`、`episodes(ts)`、`facts(entity)`。
所有写经 `db.tx()`;状态变更单条原子 UPDATE。

### 6. 与 live bot 共存(物理隔离三件套)
- **目录**:ECS `/opt/xiaowang-v2/`(live bot 在 `/opt/esm-bot/`)。本地 `<local-repo>`,**不复用 digital-twin 任何代码**。
- **db**:`/opt/xiaowang-v2/v2.db`,绝不碰 live 的 `esm.sqlite`/`seed.sqlite`。
- **端口**:live 占 8080(企微回调)+8787(UI)。v2 CLI 阶段不占端口;企微 adapter 用 **8090**,Caddy 按 path 分流。**部署前 `ss -tlnp | grep -E '8080|8090'` 查冲突**。
- **systemd**:独立 unit `xiaowang-v2.service`(`/usr/local/bin/node /opt/xiaowang-v2/main.mjs`,`Restart=always RestartSec=5`),与 live 互不影响。

### 7. 分阶段建造(快速验证优先)
- **P0 环境探针(动手前第一件事,一条命令定生死)**:`ssh ecs-wecom 'node --version && node --experimental-sqlite -e "<建fts5+WAL+FULL测试>"'`。确认 ECS Node 版本 + FTS5 是否可用。**这步在写任何模块之前做。**
- **P1 最小可靠核(CLI,≤4 文件能跑)**:`db.mjs`+`loop.mjs`+`tools.mjs`(含 send_message/memory)+`durable.mjs`(tasks/steps/timers/outbox + worker tick)。CLI 驱动跑通"记忆召回 + 工具循环 + 派定时任务"。**验证:`kill -9` 后重启从断点续/重跑不重发;造一个 90s 后触发的 test timer 看调度→LLM→工具→outbox 全链路。**
- **P2 自愈**:`callWithResilience`(重试+jitter+Kimi fallback)接进 loop;`watchdog_cron.mjs` 进程外看门狗 + 报警。模拟 DeepSeek 429、模拟进程 hang 验证。
- **P3 企微 adapter**:8090 端口 + Caddy route,outbox relay 发企微。造 test timer 验证端到端发到子淇手机。
- **P4 部署 + 备份**:systemd unit;每日 cron `cp v2.db`(先 `wal_checkpoint`)。
- **P5(backlog,跑稳几周后按真实痛点逐个加)**:facts 自动抽取、Sawtooth 压缩、sqlite-vec 向量召回、subagent 重检索隔离、step 级 memo 扩到 agentic 任务、attempt_notes。**一个不在首版。**

### 8. 故意不做(为删除而构建 + 少即是多)
不装 Temporal/DBOS/Kafka/Redis/向量库 server;不引 LangChain/Vercel SDK;不上多 agent 框架;不上三态熔断/dead_letter 独立表/OTel collector/Ralph Loop/每轮自动抽取/sqlite-vec/better-sqlite3;**不配 WatchdogSec**;不做并行 step/workflow 版本化/分布式 saga;不复用 digital-twin 代码;不靠 prompt 实现任何超能力。

---

## 技术栈
Node 24(ECS)/22(本地) + node:sqlite 内置(WAL + synchronous=FULL,实测 FTS5 本地可用、ECS 待复测)+ 纯 fetch 调 DeepSeek/Kimi OpenAI 兼容接口(不引任何 SDK/框架)+ FTS5 全文检索(建表 try/catch 自动降级 LIKE)+ systemd Restart=always(不配 WatchdogSec)+ crontab 进程外看门狗 + 可选 pino JSON 日志。零外部中间件、零 native 模块、单进程单 .db,全卡在 2C2G 内。

## 文件清单
- `main.mjs` — 进程入口:初始化 db、注册工具、启 worker tick(调度+心跳)、挂 adapter。整个程序唯一常驻进程。
- `db.mjs` — 持久层地基:开 v2.db(WAL+synchronous=FULL+busy_timeout),建全部表+索引;FTS5 虚表用 try/catch 建,失败设 MEMORY_MODE='like';导出 tx() 事务包装器。单写连接。
- `durable.mjs` — durable 内核:tasks/steps/timers/outbox 读写 + worker tick(扫 timer 按 catchup_policy 触发、扫陈旧 task 按分级重放/整体重跑、relay outbox、写 heartbeat)。enqueueTask/scheduleTimer/recordStep/markComplete。
- `loop.mjs` — agentic 控制环:组上下文→callWithResilience 调 LLM→解析 tool_calls→经 tools wrapper 执行→回灌→护栏判终止(maxTurns20/wall-clock180s/no-progress)。约150行纯 fetch。
- `tools.mjs` — 工具注册表数组 + 副作用统一 wrapper(派 dedup_hash 内容哈希、危险操作黑名单)。首版 ≤8 工具:memory_search/write、read/write_file(沙箱)、http_get、schedule_task、send_message(走outbox)、query_db(只读参数化)。
- `memory.mjs` — 分层记忆:episodes(只追加,首版全留)/facts(半自动经staging)。retrieve() 按 MEMORY_MODE 走 FTS5 BM25×recency×entity 或 LIKE+ts 降级。注入 top-k。
- `llm.mjs` — 外部调用收口 callWithResilience():指数退避+30%jitter+封顶2次+按错误分流;DeepSeek↔Kimi 主备 fallback;不做三态熔断。纯函数。
- `adapter.mjs` — 渠道适配:CLI(stdin/stdout,起步)与企微(8090 HTTP 回调,P3接)共用接口。inbound→喂loop;outbox relay→发出站。换渠道只换此文件。
- `watchdog_cron.mjs` — 进程外看门狗(crontab每5min,不依赖主进程):查 heartbeat 表 ts,静默>10min→systemctl restart+直接调企微API报警。
- `prompt.mjs` — system prompt 组装:🦞人格(从旧小王迁)+ 召回记忆注入 + 工具说明。人格/记忆迁移用。
- `deploy/xiaowang-v2.service` — systemd unit:/usr/local/bin/node main.mjs,Restart=always RestartSec=5,WorkingDirectory=/opt/xiaowang-v2。不配 WatchdogSec。
- `deploy/install.sh` — 部署脚本:scp 目录到 /opt/xiaowang-v2、装 service、装 crontab watchdog、ss 查端口冲突、每日 cp 备份 cron。
- `deploy/probe.sh` — P0 环境探针:ssh ecs-wecom 跑 node --version + 建 fts5/WAL/FULL 测试,动手前第一件事。
- `package.json` — type:module,零运行时依赖(或仅 pino 可选)。
- `.env.example` — DEEPSEEK_KEY/KIMI_KEY/企微凭证/端口/沙箱目录/owner_id。

## 施工分工(第二轮 build swarm)

### builder-A 持久层与durable内核
文件:`db.mjs`, `durable.mjs`

db.mjs:开 v2.db,PRAGMA WAL+synchronous=FULL+busy_timeout=5000;建 tasks/steps/timers/outbox/episodes/facts/heartbeat 表+索引;FTS5 虚表 try/catch 建,失败导出 MEMORY_MODE='like' 否则 'fts5';导出 tx(fn) 事务包装器、单写连接单例。durable.mjs:依赖 db.mjs 的 tx;实现 enqueueTask/scheduleTimer/recordStep/markComplete/replayOrRerun;worker tick(setInterval 5s):扫 timers(fire_at<=now,按 catchup_policy='skip'|'once' 触发)、扫陈旧 task(确定性任务 step 级 memo 重放/agentic 任务整体重跑)、relay outbox、UPSERT heartbeat。接口:导出 {enqueueTask, scheduleTimer, startWorker, recordStep, markComplete}。outbox 去重靠 dedup_hash UNIQUE 约束。

### builder-B agentic环与LLM收口
文件:`loop.mjs`, `llm.mjs`, `tools.mjs`, `prompt.mjs`

llm.mjs:callWithResilience(fn,opts) 指数退避1→2→4s+30%jitter封顶2次,按错误码分流,DeepSeek↔Kimi fallback;chatCompletion(messages,tools) 走纯 fetch。tools.mjs:registry 数组+wrapToolCall(自动派 dedup_hash=sha256(内容)、危险操作正则黑名单、副作用工具调用前查/写 outbox dedup);首版8工具实现。prompt.mjs:buildSystemPrompt(persona,recalled)。loop.mjs:依赖 llm/tools/memory/durable;runLoop(input,session):组上下文→chatCompletion→执行 tool_calls(经 wrapToolCall)→回灌→护栏(maxTurns20/wallclock180s/no-progress hash 检测)判终止。接口:导出 runLoop(input,sessionId)→final text。注意:LLM 调用绝不在 db 事务里。

### builder-C 记忆层
文件:`memory.mjs`

依赖 db.mjs(读 MEMORY_MODE)。episodes 只追加 writeEpisode();facts 半自动 writeFactToStaging()+promoteFact();retrieve(query,k):MEMORY_MODE='fts5' 走 episodes_fts BM25×exp(-Δdays/τ)×entity boost;='like' 走 LIKE+entity+ORDER BY ts DESC LIMIT k。导出 {writeEpisode, retrieve, writeFactToStaging}。首版不做自动抽取、不做 Sawtooth。

### builder-D 渠道适配与进程编排
文件:`main.mjs`, `adapter.mjs`, `watchdog_cron.mjs`

adapter.mjs:CLI adapter(stdin readline→runLoop→stdout)起步;企微 adapter(8090 http 回调→runLoop;outbox relay 读 pending→发企微 API→标 sent)P3 接,接口 startAdapter(mode,{onMessage,pollOutbox})。main.mjs:initDb→注册工具→durable.startWorker→adapter.startAdapter,优雅退出。watchdog_cron.mjs:独立可执行,读 v2.db heartbeat 行,ts 静默>10min→child_process systemctl restart + fetch 企微报警(读 .env 凭证,不依赖主进程任何模块状态)。依赖 builder-A/B/C 的导出接口。

### builder-E 部署与探针
文件:`deploy/xiaowang-v2.service`, `deploy/install.sh`, `deploy/probe.sh`, `package.json`, `.env.example`

probe.sh:ssh ecs-wecom 跑 node --version + node --experimental-sqlite -e 建 fts5/WAL/synchronous=FULL 测试,输出 PASS/FAIL。install.sh:ss -tlnp 查 8090 冲突→scp 到 /opt/xiaowang-v2→cp .env→systemctl enable+start→装 crontab(watchdog 每5min + 每日 cp v2.db 备份前先 wal_checkpoint)。service:Restart=always RestartSec=5 无 WatchdogSec。package.json type:module 零依赖。无代码依赖,可与 A-D 并行,但 probe.sh 必须最先在 ECS 跑。

## 风险
- P0 探针没在 ECS 实测就开建 = 整个地基押在假设上。本地 Node22 实测 FTS5+WAL+FULL 全 OK,但 ECS 是 Node24 不同 build,FTS5 必须复测。live bot 已证 node:sqlite+WAL 在 ECS 可用,但没用过 FTS5。这是动手前唯一必做的硬验证。
- 幂等收口不全 = 重放给子淇连发两条企微/重复扣 token。已改用内容 hash(不用 step_seq)+ outbox.dedup_hash UNIQUE 约束,但'发了企微但没写 dedup 就崩'的窗口物理上消不掉(SQLite 事务无法和外部 HTTP 原子提交)。诚实口径:at-least-once + 尽力去重,极小概率重发,不是 effectively-once。
- DatabaseSync 同步阻塞 event loop:虽全串行可行(n=1 负载低),但若 LLM 调用误写进 db 事务里、或某次 fsync 在廉价云盘卡几十 ms,会拖慢入站。纪律:LLM/HTTP 走 async fetch 不占锁,DB 写做小事务。这是约定不是结构强制,review 时要盯。
- facts 自动抽取首版关掉了(防 MEMORY.md 旧病自动放大),但这意味着'语义记忆沉淀'这条超能力首版是半自动/降级态交付,不是全自动。诚实:首版记忆主力是 episodes 全留 + 召回,facts 是手动喂。
- agentic LLM 任务首版走'整体重跑+内容hash去重'而非精确断点续——长 agentic 任务崩溃后会重头跑(浪费 token,但不重发副作用)。'断点续'这条超能力首版只对确定性 timer 任务成立,对 LLM 多步任务是保守降级。这是对'durable replay 套非确定性 LLM 不安全'的正面让步,不藏。
- FTS5 降级到 LIKE 后,记忆召回退化成关键词包含+时间排序,丢了 BM25 相关性。n=1 几千条能跑但召回质量打折。只在 ECS FTS5 不可用时才触发,实测大概率不触发。
- worker tick 全串行:若某次 tick 里 outbox relay 发企微卡住(http_get 无 timeout),会延后下一 tick 心跳→可能触发进程外看门狗误判。所有外部调用必须带 timeout。
- 端口/目录隔离靠纪律 + install.sh 的 ss 检查,不是结构强制。部署时若漏查,8090 撞 live 或误碰 esm.sqlite 会污染 live bot。install.sh 必须先 ss -tlnp 再启。

## 诚实账(不哄)
真前沿(frontier,有据但生产案例少):① SQLite 进程内 durable execution(步骤 memo+重放),Morling/DBOS/Honker 多方实证,这块思想扎实、值得抄,但我**首版只对确定性 timer 任务用 step 级 memo,对 agentic LLM 任务退到整体重跑+内容去重**——因为 durable replay 套在非确定性 LLM 上的精确断点续,两个红队都正确指出三套原稿都没真正解决(LLM 重放可能产出不同 tool_call 序列),我不假装解决了。② Facts-as-First-Class / Sawtooth 压缩 / 自动抽取——全进 backlog,首版一个不上,这些是 2026 论文级、生产少,首版上 = 拿子淇当 burn-in 小白鼠还污染记忆。\n\n务实取舍(established 或刻意降级):手写 loop(不引框架)、纯 fetch、FTS5(降级 LIKE)、systemd Restart=always、重试+jitter+Kimi fallback(不上三态熔断)、进程外 cron 看门狗(不配 WatchdogSec)、报警复用企微。这些都是成熟模式或对 n=1 的正确减法。\n\n'effectively-once'是假的:已改口径成 at-least-once + 尽力去重。这是两个红队都点破的诚实性缺陷,原三套都把工程边界说漂亮了,我改了。\n\n建出来要多少 burn-in 才敢叫'稳':P1 核心环写完(乐观 1-2 天)只能叫'CLI 跑通'。要叫'稳',需要:① ECS 上真实挂 systemd 跑**连续 7 个晚上不崩**,看 heartbeat 日志无断点;② 至少经历 3-5 次真实 DeepSeek 抖动/429,确认 fallback 和重试真的兜住;③ 至少 2 次主动 kill -9 验证重启后不丢任务、不重发企微;④ 派一个真实跨天任务('3天后提醒'),关机一晚再开,验证补跑语义对。**没跑满这一周 burn-in 之前,任何'durable 工业级扎实'的说法都是纸上的。**\n\n最可能翻车的三处:(1) **ECS FTS5 没实测就开建**——P0 探针不做就动手是最大风险,务必第一条命令先验。(2) **幂等内容 hash 在 send_message 上覆盖不全**——任何一个有副作用的工具忘了走 wrapper,重放就重发,必须 review 每个 sideEffect:true 工具都过了 dedup。(3) **全串行架构下某个外部调用没带 timeout**——一个卡住的 http 拖垮 tick 心跳,连锁触发看门狗重启,看起来像随机崩溃、极难排查。这三个都是结构/纪律边界,不是聪明能补的,build 时要当 first-class 盯。\n\n最后一句不哄的话:这份蓝图相对三套原稿的主要价值不是'更前沿',是**砍得更狠 + 把三套都没解决的 LLM-durable 缝合漏洞正面承认并降级**。它能不能真稳,100% 取决于那一周 ECS burn-in,不取决于蓝图写得多漂亮。"

---
*本蓝图由多 agent 工作流产出:5 路前沿研究(34 个模式)→ 3 套候选架构 → 2 个红队逐条拆台 → 总架构师收口合成。2026-06-25。*
