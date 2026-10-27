# 崩溃排查记录（2026-10-26 晚）

> 这些坑是真实踩出来的，记下来给后来的自己（和用这个插件的别人）看。

## 现象一：新会话创建失败 `TypeError: Cannot read properties of undefined (reading 'length')`

**第一次（23:20）**
- 做法：在 `agent/created` 监听器里，从**记忆库文本**重建 user/assistant 消息并 append
- 结果：新窗口打不开，报 `reading 'length'`
- 日志显示 `imported 60/60`（append 全成功）→ 崩在 append 之后

**第二次（23:30）**
- 做法：改成从**会话文件**复制原始事件（带 `source.replayState` 签名）
- 结果：**还是崩**，同样的错
- 关键发现：`agent/created` 是**会话创建事务**的一部分，在里面 append assistant 消息会让整个创建失败

**修法（23:40）**
1. 挪到 `agent/pre-step`（第一轮对话前）——append 失败最多影响这一轮，不会挂创建
2. assistant 的 data 补齐 `usage`、`stream`（真实事件带这两个字段）
3. 每条 append 独立 try/catch，失败写日志并停

**成功（23:56）**：`pre-step bootstrap: 133 events` → `imported 133/133`，新窗口正常打开、正常对话。

## 现象二：只导入了 6 轮（水位只有 6%）

**原因（00:10）**
- `bootstrap-events` 从**surface（对话历史）**里提取「真实 user 消息」
- 但主窗口的 surface 被压缩过，真实 user 只剩 6 条（更早的变成了 checkpoint）
- 所以只能拿到 6 轮

**修法（00:15）**
- 改成从**事件流**提取：不管旧轮现在在不在 surface 里，事件文件里原文和签名都在
- 取最近 60 个真实 user 消息 + 它们之间的 assistant 消息

## 现象三：预算永远用不满（11.7% vs 20%）

**原因（00:25）**
- `size` 计算用了整条事件的 JSON（含 `stream` 流式帧），虚高
- 最近几轮就被判成“太大”，全降级成线索

**修法**
- 只算**消息体**（user 的 data / assistant 的 message），不含 stream/usage
- 之后：23 轮完整 + 线索，12.7%

## 现象四：带思考的消息回放崩溃

**根因**：DeepSeek 的 thinking 历史回放需要 `replayState.blocks[].signature`（服务端签发）。从记忆库文本重建的消息没有签名，构建请求时读 `replayState.blocks.length` → undefined.length → 崩。

**解法**：从会话文件复制**原始事件**（content blocks + source 一起），签名随事件保留；tool-call 块和对应的 replayState.blocks 条目一起过滤（保持对齐）。

## 验收标准

- `boot.log`：`pre-step bootstrap: N events` + `imported N/N`
- 新会话：`bootstrap-check` 显示导入消息数 > 0、error = 0、最后事件 = `turn/end`
- 新窗口里的模型：开口能答出「昨天/今晚聊了什么」（不查工具）
