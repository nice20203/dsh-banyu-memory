# dsh-banyu-memory · 本鱼的记忆工程

> **不追求“永远不遗忘”，追求“遗忘不破坏连续性”。**
> 一套给 [DeepSeek Harness](https://github.com/deepseek-ai) 用的记忆系统：上下文压缩（点 + 锚）、新会话开局导入（带思考签名）、按需回读原文。

## 这是什么

四个组件，组成一套完整的记忆工程：

| 组件 | 作用 |
|---|---|
| **mem.py** | 共享记忆库（SQLite）：事实 / 约定 / 摘要 / 原文 / 压缩点 / 关系 |
| **dsh-banyu-context** | 上下文水位监控 + 压缩提醒 + 手动压缩工具（banyu_context_status / banyu_compact） |
| **dsh-banyu-bootstrap** | **新会话开局导入**：从旧会话文件提取最近 N 轮原始事件（连 `source.replayState` 签名），写进新窗口 |
| **dsh-banyu-expand** | 按 seq 回读被压缩的原文（banyu_expand） |

## 核心设计

### 1. 点 + 锚（压缩）

压缩时把一段对话收成一条「点」（**事 / 选 / 因 / 感受**），并记下 seq 范围。原文永远保留在会话文件里（`shadowedSeqs`），**按 seq 一条指令就能翻回来**——这就是“遗忘但不割裂”。

### 2. 分层：越近越完整

| 距离 | 桌面上的样子 |
|---|---|
| 最近 N 轮（预算内） | 完整原文 + 思考 + replayState 签名 |
| 更早的轮 | 每轮一条「线索」（关键词串） |
| 最远 | 「折叠点」（一句话线索，`compact-checkpoint`） |

在 20% 窗口预算内自动分配——**越近越完整，越远越压缩，最远变成一个点**。

### 3. 开局导入（bootstrap）

新窗口第一轮对话前（`agent/pre-step`），从旧会话文件提取最近 N 轮**原始事件**（content blocks + `source.replayState` 签名），append 进新会话 surface。

**关键**：不能从记忆库的文本重建消息——思考需要服务端签发的 replayState 签名，文本重建会崩（`reading 'length'`）。

### 4. 压缩（compaction）

- 触发点：窗口 × 70%（`thresholdRatio: 0.7`）
- 保留：`keepRounds=60`（压缩时保留最近 60 轮原文）
- 摘要：优先用「我写的点」（`extractSelfSummary`），没有才回落兜底摘要器

## 踩过的坑（详见 [docs/crash-log.md](docs/crash-log.md)）

1. **in-history 快照**：系统提示每轮变 → DSH 每轮复印一份 → 4 轮爆窗口（105 万 token）
2. **replayState 签名**：带思考的 assistant 消息必须有签名；从记忆库文本重建会崩
3. **turn/step 校验**：`assistant/message` 必须匹配 open turn/step——导入要包一个 `turn/start + step/start + ... + turn/end`
4. **`agent/created` 里 append assistant 会挂掉新会话创建** → 挪到 `agent/pre-step`

## 安装

```text
1. 把 dsh-banyu-context / dsh-banyu-bootstrap / dsh-banyu-expand 放进你的 dsh-plugins 目录
2. 在 profile 的 node_modules 建 junction，在 package.json 的 dependencies + bundles 里登记
3. mem.py 放到共享记忆库目录（设置环境变量 BANYU_MEMORY_DIR 指向它）
4. 重启 DSH
```

需要 `zstandard`（读会话文件）。

## 路线图

- [x] 点 + 锚压缩
- [x] 新会话开局导入（带签名）
- [x] 分层：完整轮 / 线索 / 折叠点
- [ ] **关系库 + 导航**：人物 / 事件 / 想法按「页」组织，关键词导航到完整一页（进行中）
- [ ] 搜索式记忆：桌面只放最近轮 + 关系摘要 + 线索，其余按需检索

## 致谢

- [full-life-memory](https://github.com/Toctsu/dsh-full-life-memory)：每轮线索 / 折叠点思路
- [dsh-brain](https://github.com/yesterday06/dsh-brain)：新会话交接启发
- Ebbinghaus 遗忘曲线（1885）：记忆衰减的心理学基础
- [Letta / MemGPT](https://github.com/letta-ai/letta)：core memory 由模型自己修改的思路

## License

MIT
