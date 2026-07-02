> 本文档由多 agent ultracode 工作流产出（22 agent / 6 路研究+审计 → 3 套候选架构 → 12 路红队 → 总架构师收口）。2026-06-25。
> 与 `BLUEPRINT.md`（v2 磐石核）并列：那份是 v2 的躯体，这份是让 v2「走到台前、像无限延续的真人」的连续性引擎设计。**设计稿，落地前需子淇拍板（见 §8）。**

## BLUEPRINT — 不可感知连续性引擎（xiaowang-v2）

> 总架构师收口稿。骨架取 **Blend 连续性引擎**（三层上下文 + 锚点台账 + 媒体一等公民），嫁接其余候选里红队认可的好点子：append-only 版本化摘要、确定性媒体附录、单一排序轴=episode.id、Caddy 影子镜像切换、schema 层发送闸 + owner/自环过滤、结构化抽取式摘要。逐条消化四个镜头（不可感知性 / 成本资源 / 持久性 / 切换安全）的 mustFix。
>
> **一句话**：让子淇在微信单窗口里感觉对面是一个无限延续、内核不变、越来越靠谱的真人。错觉 = 逐字近窗（连续感）+ 运行摘要（中期桥）+ 锚点台账（永不矛盾）+ FTS 召回 / 媒体指针（长程精确指代）四件套。任何一层塌，对应破绽暴露。**不承诺记得一切，只保证关键的 / 被指代的 / 用户在意的可召回。靠谱 > 无缝。**

---

### 0. 已核对的事实基线（不是设计假设，是读过 v2 源码确认的）

| 事实 | 位置 | 含义 |
|---|---|---|
| `handleIncoming` 调 `runAgentic` 不传 history（默认 `[]`） | main.mjs:171 | **短期记忆为零的根因**。这是第一刀。 |
| `runAgentic({history})` 已接收并以 history 起步 working | loop.mjs:52,72 | 修复只需在 main 侧把 history 传进去，loop 不用大改。 |
| tool 结果落 episodes 是扁平字符串 `name → ok`、`role='tool'`、**无 tool_call_id、无配对 assistant.tool_calls** | loop.mjs:204-211 | **裸传 role=tool 历史进 messages 会 400**。recentTurns 只能取 user/assistant 文本。 |
| `tick()` 是 main.mjs 自己的内联实现，只做 recover/timer/relay/heartbeat 四步，**从不调 runTask、没有 pending-task 执行器** | main.mjs:50-106,140 | durable.mjs 的 `tick(hooks)`/`runTask` 是生产死代码。**任何"compaction 走 createTask"的方案在当前代码里永不执行**。 |
| `buildSystemPrompt` 在 while 循环内每轮重建 | loop.mjs:99 | 常驻块成本是 **每消息 × 回合数**，不是每消息一次。 |
| FTS5 unicode61 中文整段当一 token，子串召不回，LIKE 兜底**仅在 FTS 返 0 命中时触发** | memory.mjs:118-122 | 部分命中场景媒体条会被文本条淹没。媒体需独立召回路径。 |
| `retrieve(query,k,opts)` 无 sessionId 过滤 | memory.mjs:103 | 多源 / 跨上下文错召回风险（n=1 当下低，但要可控）。 |
| `synchronous=FULL` + 单 WAL 单写连接 + `tx()` 同步防呆 | db.mjs:93,290,295 | 每次 COMMIT 一次 fsync，媒体路径写放大；LLM/fetch 严格 tx 外。 |
| fallback provider 实配 `moonshot-v1-8k`（8K 窗），llm body 无 max_tokens 无截断 | .env.example / llm.mjs | 常驻块每条加 token，多轮放大可溢 8K → 400。 |

**结论**：三个候选都把"先修 main.mjs:171"列为最高 ROI 第一刀——这条无争议。但 SleepCompaction 的"compaction 走 durable task"在当前代码是空转（fatal），所以本蓝图的后台压缩 **不走 task 机器，走独立 `setInterval`**，与 worker tick 物理解耦。

---

### 1. 不可感知连续性引擎：完整机制

#### 1.1 五层上下文，职责正交、注入路径物理隔离

| 层 | 载体 | 防的 tell | 是否参与有损压缩 |
|---|---|---|---|
| **PERSONA(🦞)** | prompt.mjs 硬编常量，只读 | #7 人格漂移 | **否**（永不进任何可变层）|
| **锚点台账 pinned facts** | `facts.pinned=1` 子集，确定性全量注入（~12 条上限）| #3 自相矛盾 | **否**（与对话流物理剥离）|
| **逐字近窗 recentTurns** | episodes 最近 N 轮 user/assistant 原文 | #2 忘刚说的 / #4 重复问 / #6 当下指代 | **否**（永远逐字）|
| **运行摘要 running_summary** | `session_summaries` 表，版本化 append-only | #1 重置感（中期桥）| **是**（但只压"中段"，见下）|
| **FTS 召回 + 媒体指针** | episodes / media_log | #5 媒体指代 / #6 远程指代 | **否**（原文留底）|

#### 1.2 在线读路径（每条消息，零额外 LLM 调用，纯 SQLite）

```
// main.mjs handleIncoming —— 注意顺序：先组 ctx，再 append user，避免双注入
async function handleIncoming({ sessionId, userInput }) {
  // ① 先组装上下文（此刻当前 userInput 还没进库 → recentTurns 不含它，杜绝双注入）
  const ctx = assembleContext(sessionId, userInput);   // 纯同步 SQLite 读
  // ② 再落 user episode（拿到 id 作为本轮上界，后续压缩边界用）
  appendEpisode({ sessionId, role: 'user', content: String(userInput ?? '') });
  // ③ 跑 agentic，history = 逐字近窗
  let reply;
  try {
    const out = await runAgentic({
      taskId: null, sessionId, userInput,
      history: ctx.recentTurns,        // ← 修 main.mjs:171 amnesia
      summary: ctx.summary,            // 运行摘要文本
      anchors: ctx.anchors,            // 锚点台账
      recalled: ctx.recalled,          // 已过相关性闸
      recallWeak: ctx.recallWeak,      // 召回置信度可见化标志
    });
    reply = out?.reply ?? '';
  } catch (e) {
    console.error('[main] runAgentic failed: %s', e.message);
    reply = '（我这会儿卡了一下，稍后再试。）';   // 注意：这句本身是 tell #1，尽量靠结构避免触发
  }
  appendEpisode({ sessionId, role: 'assistant', content: String(reply ?? '') });
  return reply;
}

// context.mjs 新模块（可整块删除）
function assembleContext(sessionId, userInput) {
  const sum = getSummary(sessionId);   // session_summaries 最新有效行；无则 {summary:'', covers_until_id:0}

  // 1) 逐字近窗：只取 user/assistant 文本（绝不取 role='tool'，否则裸 tool 消息 400）
  //    单一排序轴 = episodes.id（全局 AUTOINCREMENT 单调）；id > covers_until_id 保证不与摘要段重叠
  const rows = db.prepare(`
    SELECT id, role, content FROM episodes
    WHERE session_id=? AND id > ? AND role IN ('user','assistant')
    ORDER BY id DESC LIMIT ?
  `).all(sessionId, sum.covers_until_id, RECENT_TURNS_LIMIT /*=20 条 user/assistant≈10轮*/);
  const recentTurns = rows.reverse().map(r => ({ role: r.role, content: r.content }));

  // 2) 锚点台账：facts.pinned=1 全量（极少 token）
  const anchors = pinnedFacts();   // facts WHERE pinned=1 ORDER BY importance DESC

  // 3) 语义召回 + 相关性闸 + 媒体独立配额
  let recalled = retrieve(userInput, 8, { sessionId });          // SQL 下推 sessionId 过滤
  recalled = recalled.filter(r => relevanceGate(r, userInput));  // 实体重叠+条目年龄低分丢
  const mediaHits = retrieveMedia(userInput, 2);                 // 媒体专项召回（见 2.3），保证不被文本淹
  recalled = dedupMerge(recalled, mediaHits);
  const recallWeak = recalled.length > 0 && Math.max(...recalled.map(r=>r.score)) < HEDGE_THRESHOLD;

  return { recentTurns, summary: sum.summary, anchors, recalled, recallWeak };
}
```

```
// prompt.mjs buildSystemPrompt({anchors, summary, recalled, recallWeak})
// 顺序对抗 lost-in-the-middle（高价值头尾，摘要紧跟锚点不埋中间死区）：
//   PERSONA
//   + '# 关于子淇（钉死，不会过时）\n' + anchors        ← 头部强位
//   + (summary ? '# 早先对话脉络\n' + summary : '')     ← 高价值背景
//   + '［更早细节已并入上面脉络，需要原文我可以翻记录］'   ← placeholder（指令区，禁止向用户复述此机制）
//   + (recallWeak ? '# 相关记忆（匹配较弱，不确定）' : '# 相关记忆（仅供参考）') + recalled
// messages = [system] + recentTurns(逐字) + {role:user, content:userInput}  ← userInput 永远在最末强位

// 性能修：把 buildSystemPrompt 移到 while 循环【外】，messages[0] 单次构建后复用。
// 单次 runAgentic 内 anchors/summary/recalled 冻结，每轮重拼是无谓 token 重传（3Mbps 上是真延迟）。
// 这同时让 system 前缀稳定 → DeepSeek context caching 可命中，常驻块成本被大幅抵消。
```

**双注入修复（红队 major）**：`assembleContext` 在 `appendEpisode(user)` **之前**调用 → recentTurns 物理上不含当前 userInput；userInput 只在 `buildMessages` 末尾注入一次。selftest 断言：连发两条消息，messages 里当前 userInput 只出现一次。

#### 1.3 离线写路径（压缩）—— 独立 setInterval，不进 worker tick

```
// compaction.mjs 新模块（可整块删除）。由独立低频 timer 驱动，NOT worker tick。
let _compacting = false;
const compTimer = setInterval(() => {
  if (_compacting) return;
  maybeCompact().catch(e => console.error('[compaction] %s', e.message));
}, COMPACT_INTERVAL_MS /*=60_000*/);
compTimer.unref?.();

async function maybeCompact() {
  for (const sessionId of activeSessions()) {
    const sum = getSummary(sessionId);
    // 确定性触发（不靠 LLM 判断）：
    //   (a) 逐字窗将溢出（未摘 user/assistant 轮数 > RECENT_TURNS_LIMIT + 缓冲）
    //   (b) 且 距上次入站 > IDLE_MS（只在子淇不活跃时压，让出在线带宽 + 避开 fallback 抢配额）
    if (!shouldCompact(sessionId, sum)) continue;

    _compacting = true;
    try {
      // 待压缩 = 摘要边界 → 逐字窗下沿之间的"中段"
      const floorId = nthNewestUserAssistantId(sessionId, RECENT_TURNS_LIMIT);
      const mid = db.prepare(`SELECT id, role, content FROM episodes
        WHERE session_id=? AND id>? AND id<=? AND role IN ('user','assistant','tool')
        ORDER BY id ASC`).all(sessionId, sum.covers_until_id, floorId);
      if (mid.length === 0) continue;

      // 结构化抽取式摘要（抗漂移），便宜模型 DeepSeek，硬 wallclock，retries=0
      const handoff = await withWallclock(COMPACT_WALLCLOCK_MS /*=25s*/, () =>
        chatCompletion({ messages: HANDOFF_MESSAGES(sum.summary, mid), _provider: 'deepseek', retries: 0 }));

      // === 确定性后校验（不靠摘要自觉）===
      // 1) 专名集合：压缩前出现的专名 ⊆ 压缩后保留（缺失则拒绝该摘要、保留旧版、记 system episode 告警）
      // 2) media 锚点：把 mid 里出现的 [media#N] 集合【代码确定性附加】到摘要末尾一行
      //    '［涉及媒体: #42 #51］'，无论 LLM 正文是否保留 → 指代靠确定性附录兜底，不靠 LLM 听话
      // 3) 人格闸：正则扫摘要是否含 PERSONA 特征词（🦞/温水 等），命中则拒写、保留旧版、告警
      if (!passesGuards(handoff, mid, sum.summary)) { markNeedsReview(sessionId); continue; }
      const finalSummary = appendMediaAppendix(handoff, mid);

      // === append-only 版本化写入（不是覆盖式 UPSERT）===
      // 崩溃重放生成不同摘要时两版都在库、可对账；buildSystemPrompt 永远读最新有效行
      const newCoverUntil = mid[mid.length-1].id;
      tx(c => {
        c.prepare(`UPDATE session_summaries SET superseded=1
                   WHERE session_id=? AND superseded=0`).run(sessionId);
        c.prepare(`INSERT INTO session_summaries
          (session_id, summary, covers_until_id, superseded, needs_review, updated_at)
          VALUES (?,?,?,0,0,?)`).run(sessionId, finalSummary, newCoverUntil, nowMs());
        // 同一 tx 写观测痕迹，状态与观测强一致
        c.prepare(`INSERT INTO episodes (ts,session_id,role,content,entity,task_id)
          VALUES (?,?,?,?,?,?)`).run(nowMs(), sessionId, 'system',
          `[compacted ${mid.length} turns → cover_until=${newCoverUntil}]`, 'summary', null);
      });
    } finally { _compacting = false; }
  }
}
```

**关键不变量**：
- **单一排序轴 = episodes.id**。recentTurns / floorId / covers_until_id / 压缩段全用 id 比较。`episode.ts` 一律 `nowMs()`，**禁止媒体管线回填历史 ts**（ts 只进 media_log）。selftest 断言"压缩段 ∩ 逐字窗 = ∅"。
- **不递归**。摘要 prompt 永远是"旧摘要 + 新中段原文"，旧摘要参与但不被"摘要的摘要"反复有损再编码。事实保真交锚点台账，摘要只管叙事连贯。
- **死循环硬护栏**：同一 covers_until_id 连续触发压缩 >3 次仍校验失败 → 强制推进 covers_until_id（接受这次摘要质量降级）+ 记告警 episode。杜绝"媒体校验过严 → covers 永不推进 → 每分钟烧 DeepSeek + 中段无界膨胀"的 fatal。

#### 1.4 七个 tell 的逐条结构性防法（靠结构，不靠 prompt）

| tell | 防法 | 结构机制 |
|---|---|---|
| #1 宣告重置 | 压缩对在线透明；摘要是"交接班笔记"无缝续上；placeholder 放指令区并显式禁止向用户复述"折叠/摘要/记录"等内部词 | 模型无"重置"触发点；元信息泄露被指令隔离 |
| #2 忘刚说的 | recentTurns 逐字窗（修 main.mjs:171）。压缩边界永远落在最近 N 轮之前 | 纯 SQLite 查，零模型 |
| #3 自相矛盾 | 锚点台账 `pinned=1` 确定性全量注入、永不进有损摘要 + 召回相关性闸滤旧错召回 + 摘要后专名集合校验。**诚实降级：anchor 入口半自动，覆盖率有限，标 emerging** | 硬状态物理剥离对话流 |
| #4 重复问已答 | 窗内天然在场；超窗靠摘要保留"已决事项"+ 锚点保留偏好 | 逐字窗 + 抽取式承诺 |
| #5 媒体指代 | media_log 两层 + 描述进 episodes + **媒体专项召回配额（retrieveMedia）不被文本淹** + [media#N] 确定性附录 | 不转文本则 FTS 永召不回；独立召回路径 |
| #6 跨边界指代断裂 | 窗内 transformer 自带消解；超窗靠 HANDOFF"禁代词保专名"+ [media#N] token + 召回。**ts→id 单轴防边界错位** | 专名/媒体锚点字符串保留 |
| #7 人格漂移 | PERSONA 常量永不进可压缩层（v2 已做对）+ 摘要写库前正则人格闸拒写 | 人格不进任何有损路径 |

**诚实护栏（反向破绽）**：`recallWeak` 时召回段标"匹配较弱"，模型自然 hedge："这事我印象里提过，细节记不准，你说下"（🦞克制语气，非卖萌，原则10）。宁可诚实"翻一下记录"，不编不可恢复的假连续。措辞与 PERSONA 一起 code review。

---

### 2. 媒体统一为可召回的一等上下文

#### 2.1 表示（迁 live esm-bot 已实证链路，搬运非发明）

两层：**不可逆原始字节落盘**（media/ 目录，原则9 绝不自动删）+ **可再生文本描述**（图走 kimi 视觉、语音走讯飞 ASR）。

#### 2.2 入库（复用现成召回，零新召回路径 + 一条独立配额路径）

- 描述写普通 episode：`content='[media#42|图片] 2026-06-18 子淇发了一碗牛肉面配煎蛋'`，`entity='media'`，`task_id=media_log.id`（复用闲置列，零 episodes schema 变更）。
- **[media#N] 不作 FTS 召回 key**（unicode61 会把 `media`/`42`/`#` 拆坏）。媒体召回走 `retrieveMedia(query,k)`：`entity='media'` 的 episodes 上做 LIKE 子串 + media_log 的 session/ts 索引直查，**单独配额并进 recalled**，保证不被文本条淹没（消化红队"FTS 部分命中淹没媒体"的 major）。
- [media#N] token 只作 LLM 上下文里的稳定指代标记 + 压缩附录锚点。

#### 2.3 召回只注入文本描述，绝不注入 base64 原图（原则8 + 2C2G/3Mbps 硬约束）

"上周那张沙拉照" = `retrieveMedia('沙拉')` 命中描述 episode，注入描述文本。原图按需：首版不加工具；真出现"描述不够要重看清图"痛点再加第 9 工具 `recall_media(media_id)`（8→9 仍 ≪15）。

#### 2.4 语音零特殊待遇 + 失败要响

ASR 出文本当文本走主链路。原始音频两层留存，ASR 失败 transcript=null 但 file_path 永远有。`media.mjs` 启动探测 ffmpeg；不可用 → 响亮告警 + 语音给明确回执"语音暂不能转写"，**不静默 transcript=null**（消化"整类语音静默失忆"的 major）。

#### 2.5 迁移最小集

图片视觉（kimi 纯 HTTP，零依赖）**首版必迁**；语音 ASR（silk-wasm 是 wasm 非 native 可接受，ffmpeg 是命令行外部进程需 ECS 确认已装）**首版迁、ffmpeg 缺则降级落盘**；文件解析（pdf/docx 可能引 native）**砍进 backlog**。

---

### 3. 改动清单（file + 表 + 列）

#### 3.1 Schema（db.mjs initDb，全部 IF NOT EXISTS / ALTER 幂等）

| 对象 | 变更 |
|---|---|
| **新表 `session_summaries`** | `id PK AUTOINCREMENT, session_id TEXT, summary TEXT NOT NULL DEFAULT '', covers_until_id INTEGER NOT NULL DEFAULT 0, superseded INTEGER NOT NULL DEFAULT 0, needs_review INTEGER NOT NULL DEFAULT 0, updated_at INTEGER`。**append-only 版本化**，最新有效行 = `superseded=0 AND needs_review=0`。索引 `idx_summaries_session ON (session_id, superseded)`。 |
| **新表 `media_log`** | `id PK AUTOINCREMENT(=[media#N]锚点), ts, session_id, sender_id, kind CHECK(image|voice|file), file_path TEXT NOT NULL(不可逆), transcript TEXT(可再生,失败null), model TEXT, coded_at INTEGER`。索引 `idx_media_session ON (session_id, ts)`。 |
| **`facts` 加列** | `pinned INTEGER NOT NULL DEFAULT 0`（锚点台账子集）。索引 `idx_facts_pinned ON (pinned)`。 |
| **`facts` 启用闲置列** | `last_accessed/access_count` 仅作观测写（首版**不进召回排序**，避免正反馈锁死；保留 recency τ=14 主导）。 |
| **`episodes` 加索引** | `idx_episodes_session_id ON (session_id, id)`（recentTurns / 压缩 / sessionId 过滤 / COUNT 触发判定都走它）。**不改 episodes schema**（media_id 复用 task_id 列）。 |

表数 8→10。

#### 3.2 文件改动

| 文件 | 改什么 |
|---|---|
| **main.mjs** | `handleIncoming`：先 `assembleContext`（在 appendEpisode(user) 前）→ 传 `history/summary/anchors/recalled/recallWeak` 给 runAgentic。**启动独立 compaction setInterval**（不进 worker tick）。 |
| **loop.mjs** | `runAgentic` 接收已组好的 `{history, summary, anchors, recalled, recallWeak}`，删掉内部 retrieve/topFacts（移到 context.mjs）。`working` 从 recentTurns 起步。**`buildSystemPrompt` 移到 while 循环外**，messages[0] 复用。 |
| **prompt.mjs** | `buildSystemPrompt({anchors,summary,recalled,recallWeak})`：新拼接顺序 + placeholder（指令区禁复述）+ 召回段标题按 recallWeak 切换。契约注释：摘要输入绝不含 PERSONA、输出绝不复述人格。 |
| **memory.mjs** | `retrieve` 加 `opts.sessionId`（**SQL 下推** WHERE，不 post-filter）；新增 `retrieveMedia(query,k)`、`pinnedFacts()`、`getSummary(sessionId)`、`relevanceGate(r,q)`；召回命中更新 `last_accessed/access_count`（仅观测）。 |
| **adapter.mjs** | `extractWecomText` 旁加 `extractWecomMedia`：识别 msgType 7/14/101(图)/16(语音)，不再静默 return → 调 media.mjs。补 **owner 门禁**（senderId≠OWNER_ID 直接 return）+ **自环过滤**（senderId===userId 跳过）+ **CALLBACK_SECRET 路径**。debounce 合并快速连发（迁 live 4s）。 |
| **db.mjs** | initDb 加建表 / ALTER；常量区加 `RECENT_TURNS_LIMIT=20, COMPACT_INTERVAL_MS=60000, COMPACT_WALLCLOCK_MS=25000, IDLE_MS=60000, HEDGE_THRESHOLD, SUMMARY_TOKEN_CAP=800`。 |
| **llm.mjs** | body 加 `max_tokens` + **messages 预算闸**（按 provider 窗口，含 moonshot-v1-8k 的 8K；超窗按 recentTurns→recalled→summary→anchors→persona 顺序砍）。这条独立于本设计，本就该有。 |

#### 3.3 新模块（每个可整块删除——原则6 为删除而构建）

| 文件 | 职责 |
|---|---|
| **context.mjs** | `assembleContext(sessionId,userInput)`：纯同步 SQLite 组五层。`relevanceGate`。selftest 断言四层组装、placeholder、相关性闸、当前 userInput 不双注入。 |
| **compaction.mjs** | 独立 setInterval 驱动 `maybeCompact`。确定性触发 + 结构化抽取摘要 + 三道后校验闸 + append-only 版本化写 + 死循环硬护栏。`HANDOFF_MESSAGES` 模板。selftest（mock LLM）：触发条件、covers 推进、压缩段∩逐字窗=∅、专名/media/人格闸、append-only 版本化、短 session 不压缩。 |
| **media.mjs** | `handleMedia`：downloadMedia 三路兜底落盘 → kimi 视觉 / 讯飞 ASR → media_log 两层 → appendEpisode([media#N])。**串行队列**（一次一个 ASR/ffmpeg，ffmpeg --threads 1 + timeout）。ffmpeg 启动探测。 |
| **asr.mjs** | 讯飞链（SILK→silk-wasm→ffmpeg 16k→讯飞），从 live 迁。 |

---

### 4. Burn-in 安全切换计划（全程不劫持 / 不拖垮 live esm-bot）

> **核心事实**（消化 switch-safety fatal）：e云企微回调到 ECS 后是 **Caddy 按 path 分流** 到 8080(live)/8090(v2)；但 e云平台层 token 只有**一个回调 URL 槽**，且 v2/live 共用同一 QIWE_GUID/token。**绝不去 e云 Token Center 改 URL**（= 硬切换 = 断 live 收信，踩穿 MEMORY 红线）。安全路线 = **Caddy 影子镜像 + v2 发送硬关**。

| 步骤 | 动作 | 回滚 |
|---|---|---|
| **S0 内部修复（不碰收信/发送/live）** | 先单独上 **第一刀**：修 main.mjs:171 传 recentTurns + context.mjs。在 v2 私有 cli/test session 验证。这一刀纯 v2 内部，零切换风险。 | 删 context.mjs，loop 退回纯召回。 |
| **S1 v2 入站加固** | adapter 补 owner 门禁 + 自环过滤 + CALLBACK_SECRET；让 v2 能正确解析 live 回调格式（data **数组** / cmd 15000 / msgType）。**仍不接真实回调**。 | 代码 revert，v2 仍只发不收（现状）。 |
| **S2 发送硬关（schema 层，非 prompt）** | v2 从 TOOLS 注册表删 `send_message` + `enqueueOutbox` 出口 + 设 `V2_OUTBOUND_DISABLED=1`（sendWecom 入口直接 return ok 不真发）。**双闸**。 | 改环境变量恢复。 |
| **S3 影子镜像** | **Caddy 加 mirror**：回调同时转发 live:8080(主，真回子淇) + v2:8090(镜像，只入库不发)。媒体下载票据(fileAuthkey 一次性)**单一所有权**：Caddy 镜像按 msgType **只镜像文本给 v2**，媒体仍只 live 下载（或 v2 影子期对媒体只记 inbound_log 不消费票据）。 | 删 Caddy mirror 块 + reload，live 回单收。秒级。 |
| **S4 影子观察（7 晚）** | v2 在真实流量下跑 context 组装 / 压缩 / 召回。query_db 验 session_summaries 生成、recentTurns 正确、零真实 sendText（grep live 日志确认只 live 在发）。heartbeat 连续不断。 | 同 S3。 |
| **S5 历史回填 runbook** | 切主力前一次性：live `conversation`/`media_log`/MEMORY 关键事实 → 转写进 v2 episodes（重键 `wecom:{senderId}`）+ 触发初始 compaction 生成 summary + 灌红线/承诺/当前项目进 `facts(pinned=1)`。**否则切换瞬间 v2 对全部历史失忆 = 一次性引爆 #1/#2/#5/#6**。 | 切换前演练，不动 live 数据。 |
| **S6 切主力** | Caddy 把"发"的所有权从 live 切到 v2（v2 解 V2_OUTBOUND_DISABLED + 加回 send_message；live 降级为纯转发或停）。媒体下载票据所有权移交 v2。 | Caddy 配置反向 + reload，live 重新当主。秒级、有日志、可脚本化。 |

**凭证迁移**：只从 live .env 复制需要的几个 key（QIWE/讯飞/kimi）到 v2 .env（600 权限），**绝不整份 cat、绝不编辑 live .env**。上线前在 ECS 跑凭证自检（真连一次 kimi/讯飞断言非空），失败响亮报警。

---

### 5. 分阶段建造（每阶段几分钟内可验证，原则4）

| 阶段 | 交付 | 几分钟内验证 |
|---|---|---|
| **P0 第一刀（最高 ROI）** | context.mjs + main 传 recentTurns + sessionId 过滤 + relevanceGate + recallWeak hedge | cli 连发两条："提醒我下午三点开会" → "刚那个几点？" 第二句答得上且 userInput 不双注入（selftest 断言）。 |
| **P1 锚点台账** | facts.pinned 列 + pinnedFacts() + 注入。灌几条红线/承诺。 | query_db 看 pinned facts 全量注入；问一件 pinned 的事不矛盾。 |
| **P2 媒体闭环** | media_log + media.mjs + asr.mjs + adapter 媒体分流 + retrieveMedia | **私有 session** 发一图一语音 → query_db 看 media_log 两层 + episodes 带 [media#N] → retrieveMedia('关键词') 命中。ffmpeg 探测过。 |
| **P3 运行摘要** | session_summaries + compaction.mjs 独立 setInterval + 三闸 + 版本化 | 私有 session 发 25+ 条触发阈值 → 60s 内 query_db 看 session_summaries 出现 superseded=0 行 + 观测 episode；人工看 2-3 条真实摘要质量再放量。 |
| **P4 切换** | S1-S6 runbook | 见第 4 节每步验证。 |

#### 首版砍掉进 backlog（原则6/7）
- 递归 / 分层（L2 day / L3 topic）摘要——每层放大漂移。
- 本地向量库（sqlite-vec 破零 native 铁律）。
- 双 agent sleep-time（2x 成本）。
- 自动 facts 抽取每轮跑（拖垮 3Mbps；首版仍半自动 staging）。
- 文档 / 表格解析（可能引 native）。
- `recall_media` 第 9 工具（先只注入描述）。
- 物理遗忘（删 episode）——append-only 留底是抗漂移根本；salience 首版仅观测。
- 自编辑记忆块 + 在线矛盾闸（MemoryBlocks 派的自编辑漂移风险最高，砍）。

---

### 6. 诚实账

**production-real（已落地 / live 已实证，可直接搬）**：
- 三层组装（近窗+摘要+锚点）、单层 rolling summary（复刻 Anthropic compaction 交接班结构，DeepSeek 手写）、PERSONA 常量化、lost-in-the-middle 重排、placeholder 防幻觉。
- 媒体两层（原始字节+kimi 描述/讯飞 ASR）归一文本进 FTS——live esm-bot 已在同台 ECS 跑通整链。
- sessionId SQL 下推过滤、append-only 版本化摘要、单写 WAL + tx 同步防呆 + outbox dedup。
- Caddy 影子镜像切换——项目 infra 本就支持。

**emerging（机制对、参数 / 阈值要真实数据迭代，拿子淇当 burn-in）**：
- 相关性闸阈值、HEDGE_THRESHOLD、压缩触发轮数 / IDLE_MS。
- **锚点台账对 tell #3 的覆盖率**——anchor 入口半自动 promote，绝大多数真实承诺进不了 anchor，**故 tell #3 的"确定性防线"实际覆盖有限，明确标 emerging 不标 production-real**。补救：HANDOFF 额外抽"本段明确承诺"进 facts_staging + 摘要专列"未决承诺"段。
- 结构化抽取式摘要抗漂移 + 三道后校验闸——闸是确定性的（专名集合 / 正则人格词 / media 附录），但摘要内容正确性不可对账（生成式）：**running_summary 标"不可对账、可丢、不承载硬事实"，硬事实必落 facts**。

**frontier（明确不做）**：递归摘要、向量库、双 agent、NLI 写闸 / Weibull 衰减 / 可逆对账定理 / 共指消解模型——只取机制点，用 LLM 一句话 + SQLite 实现。

#### 最可能翻车的三处
1. **摘要质量是单点**。DeepSeek 把"某客户问卷项目"压成"那个项目"或把承诺压丢 → tell #3/#6 稳态污染数天（running_summary 每轮注入）。缓解：专名集合后校验拒写 + append-only 可回退 + 上线人工 sign-off + 硬事实走 facts 不靠摘要。**这是整个方案的命门，验不过不放量。**
2. **压缩 setInterval 与在线 / fallback 抢资源**。25s 硬 wallclock + retries=0 + IDLE_MS 让路 + 独立于 worker tick（不阻塞 heartbeat/relay，杜绝 watchdog 误杀）。但 3Mbps 上压缩请求仍可能与用户消息抢带宽 → 偶发首响变慢。IDLE 闸缓解但不消除。
3. **媒体召回用词不匹配**。kimi 描述用词与子淇提问词不重合 + FTS 中文子串坑 → "上周那张沙拉照"偶发召不回。缓解：retrieveMedia 独立配额 + LIKE 子串 + 描述用"客观记录可见物体/文字/数量"prompt + 低置信 hedge。残余靠 episodes 原文永留 + 诚实承认兜底。

---

### 7. 致命纪律（守住，不破）
- ① 所有外部调用（LLM/kimi/讯飞/fetch）带 timeout。
- ② episodes append-only 原文永久留底，所有摘要 / 块从 ledger 派生、可回放。
- ③ **LLM/fetch 绝不进 tx**——压缩严格"读 / 算 / 写"三段分离，写库才进 tx 且同步。
- ④ 零 native 模块（silk-wasm 是 wasm 可接受，ffmpeg 是外部进程需 ECS 确认）。
- ⑤ 单一排序轴 = episodes.id，ts 仅用于 recency 打分。
- ⑥ burn-in 期 live 收信零中断、v2 发送 schema 层硬关。

---

### 8. 待子淇拍板的决策（每个带架构师推荐）

1. **逐字近窗大小 `RECENT_TURNS_LIMIT`**（user/assistant 条数）。越大连续感越强但每轮 token 越高、moonshot 8K fallback 下越易溢出。
   - 选项：12 条(~6 轮,省 token) / **20 条(~10 轮,对齐 live+缓冲,推荐)** / 40 条(~20 轮,fallback 风险高)
   - 推荐：**20 条** + llm.mjs 预算闸在 8K fallback 时自动砍。

2. **session 边界语义**。
   - 选项：**一个 owner 一条无限会话(sessionId=wecom:{senderId},最贴微信单窗口,推荐)** / 按天切 session
   - 推荐：**一个 owner 一条无限会话**。摘要按 id 区间滚动而非按 session 切，跨天连续不断。

3. **语音 ASR 首版是否必上**（依赖 ffmpeg 外部进程，需 ECS 确认）。
   - 选项：必上(ffmpeg 缺则阻塞上线) / **迁但 ffmpeg 缺则降级落盘+回执(推荐)** / 语音整体进 backlog
   - 推荐：**迁但可降级**。图片视觉无依赖必上；语音零丢失 + 明确回执 > 静默失忆。

4. **摘要质量人工 sign-off 强度**（摘要写坏会稳态污染数天，是命门）。
   - 选项：上线即自动放量 / **P3 后人工看 2-3 条真实摘要通过才放量(推荐)** / 每条都人工审(等于卡死)
   - 推荐：**看 2-3 条再放量**，之后靠三道确定性闸 + append-only 可回退兜底。

5. **切主力触发条件**（S4 影子观察合格线）。
   - 选项：**连续 7 晚 heartbeat 不断 + 人工抽检无破绽(推荐)** / 跑满 1 个月 / 主观觉得够好就切
   - 推荐：**7 晚 heartbeat + 抽检双卡**；S6 切换秒级可回滚，门槛不必过高。
