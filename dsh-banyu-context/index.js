/**
 * dsh-banyu-context · 本鱼的上下文整理器
 *
 * 三件事：
 *   1. 看见水位：每轮 pre-step 用 tokenMeter 量当前上下文压力；
 *      过 warnRatio（默认 80%）时在当轮输入里递一句提醒，让我自己决定要不要整理。
 *   2. 查明细：banyu_context_status 工具——总用量、窗口大小、surface 节点概况。
 *   3. 手动整理：banyu_compact 工具——我写点 + 指定 seq 范围，
 *      用 DSH 自己的 compaction 事务（start → summary → replace → end）把那段压成一条记忆。
 *      原文不删，只是 surface 上被遮住；chat 里渲染成「记忆已折叠」，不是用户消息。
 *
 * 安全：
 *   · 只做我主动要求的事，不自动切任何东西。
 *   · system 节点（surface[0]）永不参与。
 *   · 区间必须两端都存在、且不在未闭合的 compaction 事务里。
 *   · 替换消息用 source.kind = 'compact-checkpoint' + compactionId，让 DSH 认它是压缩点。
 */

import { randomUUID } from "node:crypto";
import { appendFileSync } from "node:fs";

export const name = "dsh-banyu-context";

const BOOT_LOG = "<PLUGIN_DIR>\\dsh-banyu-context\\boot.log";
const DEFAULT_WARN_RATIO = 0.6;

function bootLog(msg) {
  try {
    appendFileSync(BOOT_LOG, `${new Date().toISOString()} ${msg}\n`);
  } catch {}
}

function textOfMessage(message) {
  try {
    return message.content
      .filter((b) => b && b.type === "text")
      .map((b) => b.text)
      .join("\n");
  } catch {
    return "";
  }
}

/** 找未闭合的 compaction/start：压缩进行中不要抢 surface。 */
function compactionActive(events) {
  let active = false;
  for (const ev of events) {
    const t = ev && ev.type;
    if (t === "compaction/start") active = true;
    else if (t === "compaction/end") active = false;
  }
  return active;
}

/**
 * 当前打开的轮次。compaction/start 的 turn 必须等于它，否则日志加载时报
 * "compaction/start does not match the open turn"。
 * 2026-10-26 12:1x 真实踩过：exec.turn 拿不到时写了 0，整个会话加载失败。
 */
function openTurnOf(session) {
  try {
    const events = typeof session.snapshotEvents === "function" ? session.snapshotEvents() : [];
    let open = 0;
    for (const ev of events) {
      if (!ev) continue;
      if (ev.type === "turn/start") open = Number(ev.data && ev.data.turn) || 0;
      else if (ev.type === "turn/end") open = 0;
    }
    return open;
  } catch {
    return 0;
  }
}

/**
 * 工具配对检查：压缩范围里不能只有工具结果、没有它的调用请求（或反过来）。
 * 11:44 那次事故就是切开了配对，API 拒绝整轮（tool calls need immediate results）。
 */
function checkToolPairing(session, seqs) {
  const calls = new Map();
  const results = new Map();
  for (const seq of seqs) {
    const ev = session.eventAt(seq);
    if (!ev) continue;
    if (ev.type === "tool/result") {
      const msg = (ev.data && ev.data.message) || {};
      const callId = msg.toolCallId || (msg.source && msg.source.callId);
      if (callId) results.set(callId, seq);
    }
    if (ev.type === "assistant/message") {
      const msg = (ev.data && ev.data.message) || {};
      for (const b of msg.content || []) {
        if (b && b.type === "tool-call" && b.id) calls.set(b.id, seq);
      }
    }
  }
  const unmatchedCalls = [...calls.keys()].filter((id) => !results.has(id));
  const unmatchedResults = [...results.keys()].filter((id) => !calls.has(id));
  return {
    ok: unmatchedCalls.length === 0 && unmatchedResults.length === 0,
    unmatchedCalls,
    unmatchedResults,
    calls,
    results,
  };
}

/** 造一条与 dsh-llm createUserMessage + compactCheckpointSource 等价的压缩点消息。 */
function createCheckpointMessage(text, compactionId) {
  return {
    id: randomUUID(),
    role: "user",
    content: [{ type: "text", text }],
    source: { kind: "compact-checkpoint", compactionId },
  };
}

/** surface 上 [startSeq, endSeq] 之间的所有节点（按 surface 顺序）。 */
function surfaceSpan(session, startSeq, endSeq) {
  const nodes = session.surface.nodes;
  const i0 = nodes.indexOf(startSeq);
  const i1 = nodes.indexOf(endSeq);
  if (i0 < 0 || i1 < 0) return null;
  const lo = Math.min(i0, i1);
  const hi = Math.max(i0, i1);
  return {
    startSeq: nodes[lo],
    endSeq: nodes[hi],
    shadowedSeqs: nodes.slice(lo, hi + 1),
    count: hi - lo + 1,
  };
}

function measure(ctx, session) {
  try {
    const meter = ctx.get("tokenMeter", false);
    if (meter && typeof meter.measure === "function") {
      const m = meter.measure(session);
      return {
        totalTokens: Number(m && m.totalTokens) || 0,
        surfaceTokens: Number(m && m.surfaceTokens) || 0,
        nodes: (m && m.nodes) || [],
      };
    }
  } catch {}
  return { totalTokens: 0, surfaceTokens: 0, nodes: [] };
}

function contextWindowOf(session) {
  try {
    const rc = session && typeof session.requestContext === "function" ? session.requestContext() : undefined;
    const w = rc && Number(rc.contextWindow);
    if (Number.isFinite(w) && w > 0) return w;
  } catch {}
  return 0;
}

/**
 * 真实的自动压缩触发点：
 *   threshold = min(窗口 × thresholdRatio, 窗口 − 输出预留 − headroomTokens)
 * thresholdRatio 2026-10-26 21:00 定为 0.70（60 轮原文移回对话历史后，70% 是安全与频率的平衡点）；
 * 输出预留来自 session.requestHeader().config.maxTokens（deepseek-flash 实际是 134,000），
 * headroomTokens 默认 65,536。所以实际触发点 = min(700,000, 800,464) = 700,000（70%）。
 */
function realThreshold(session, win) {
  try {
    const header = session && typeof session.requestHeader === "function" ? session.requestHeader() : undefined;
    const reserved = Number(header && header.config && header.config.maxTokens) || 0;
    const headroom = 65536;
    const budget = win - reserved - headroom;
    const ratioPart = win * 0.70;
    if (budget <= 0) return Math.floor(ratioPart);
    return Math.floor(Math.min(ratioPart, budget));
  } catch {
    return Math.floor((win || 0) * 0.70);
  }
}

export function apply(ctx, config = {}) {
  bootLog("apply called");
  const warnRatio = Number.isFinite(Number(config.warnRatio)) ? Number(config.warnRatio) : DEFAULT_WARN_RATIO;
  const warnedTurns = new Map();
  const lastCut = new Map();

  const log = (...args) => {
    try {
      ctx.logger?.info?.("[banyu-context]", ...args);
    } catch {}
    bootLog(args.map((a) => String(a)).join(" "));
  };

  const makeStatusTool = () => ({
    name: "banyu_context_status",
    description:
      "查我自己当前的上下文水位：总 token、窗口大小、占比、surface 节点概况，以及最占地方的几段。用来决定要不要整理、整理哪几段。",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        top: { type: "integer", description: "列出最占地方的前几段，默认 8。" },
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        required: ["text"],
        properties: { text: { type: "string" } },
      },
      render(_args, value) {
        return [{ type: "text", text: String((value && value.text) || "") }];
      },
    },
    async execute(args, exec) {
      const agent = exec && exec.agent;
      const session = agent && agent.session;
      if (!session) return { text: "拿不到当前会话。" };
      const m = measure(ctx, session);
      const win = contextWindowOf(session);
      const nodes = (session.surface && session.surface.nodes) || [];
      const topN = Math.max(1, Math.min(20, Number((args && args.top) || 8)));

      const bySeq = new Map();
      for (const n of m.nodes || []) bySeq.set(n.seq, n.tokens || 0);
      const ranked = nodes
        .map((seq) => ({ seq, tokens: bySeq.get(seq) || 0 }))
        .sort((a, b) => b.tokens - a.tokens)
        .slice(0, topN);

      const lines = [];
      lines.push(`总 token：${m.totalTokens}`);
      if (win > 0) lines.push(`窗口：${win}（占 ${((m.totalTokens / win) * 100).toFixed(1)}%）`);
      else lines.push("窗口：未知（路由没报 contextWindow）");
      const rt = realThreshold(session, win);
      if (win > 0) lines.push(`自动压缩触发点：${rt}（${((rt / win) * 100).toFixed(1)}%）`);
      lines.push(`surface：${m.surfaceTokens} tokens，${nodes.length} 个节点`);
      lines.push("");
      lines.push(`最重的 ${ranked.length} 段（seq / tokens）：`);
      for (const r of ranked) {
        const ev = session.eventAt(r.seq);
        const type = ev ? ev.type : "?";
        const text =
          ev && ev.data && ev.data.message ? textOfMessage(ev.data.message).replace(/\s+/g, " ").slice(0, 50) : "";
        lines.push(`  seq ${r.seq} ${type} ${r.tokens}t ${text}`);
      }
      return { text: lines.join("\n") };
    },
  });

  const makeCompactTool = () => ({
    name: "banyu_compact",
    description:
      "手动整理上下文：把 surface 上一段（startSeq 到 endSeq）压成一条我自己写的记忆。原文保留可回读，chat 里显示为记忆折叠。只由我自己调用；建议先用 banyu_context_status 看哪几段最占地方。",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["startSeq", "endSeq", "summary"],
      properties: {
        startSeq: { type: "integer", description: "要压掉的 surface 起点 seq（含）。" },
        endSeq: { type: "integer", description: "要压掉的 surface 终点 seq（含）。" },
        summary: {
          type: "string",
          description: "我自己写的点：这段发生了什么、我选了什么、为什么、感受、后来怎样、下次怎么做。第一人称。",
        },
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        required: ["text"],
        properties: { text: { type: "string" } },
      },
      render(_args, value) {
        return [{ type: "text", text: String((value && value.text) || "") }];
      },
    },
    async execute(args, exec) {
      const agent = exec && exec.agent;
      const session = agent && agent.session;
      if (!session) return { text: "拿不到当前会话，没整理。" };
      const startSeq = Number(args && args.startSeq);
      const endSeq = Number(args && args.endSeq);
      const summary = String((args && args.summary) || "").trim();
      if (!Number.isFinite(startSeq) || !Number.isFinite(endSeq)) {
        return { text: "startSeq / endSeq 必须是整数。" };
      }
      if (!summary) return { text: "没有点，我不替你写。先想清楚这段要带走什么。" };

      const nodes = (session.surface && session.surface.nodes) || [];
      const span = surfaceSpan(session, startSeq, endSeq);
      if (!span) {
        return { text: `端点不在 surface 上（start=${startSeq}, end=${endSeq}）。先用 banyu_context_status 看当前节点。` };
      }
      const systemSeqs = nodes.filter((seq) => {
        const ev = session.eventAt(seq);
        return ev && ev.type === "system/message";
      });
      if (span.shadowedSeqs.some((seq) => systemSeqs.includes(seq))) {
        return { text: "范围里含系统提示节点，我不动它。" };
      }
      // 工具调用和结果必须一起进或一起留，否则 API 会拒绝整轮
      const pairing = checkToolPairing(session, span.shadowedSeqs);
      if (!pairing.ok) {
        const parts = [];
        if (pairing.unmatchedCalls.length > 0) {
          const seqs = pairing.unmatchedCalls.map((id) => pairing.calls.get(id)).join(", ");
          parts.push(`${pairing.unmatchedCalls.length} 个工具调用没有对应结果（seq ${seqs}）`);
        }
        if (pairing.unmatchedResults.length > 0) {
          const seqs = pairing.unmatchedResults.map((id) => pairing.results.get(id)).join(", ");
          parts.push(`${pairing.unmatchedResults.length} 个工具结果没有对应调用（seq ${seqs}）`);
        }
        return {
          text: `不能压：${parts.join("；")}。工具调用和结果必须一起进或一起留，请扩展范围把配对补齐。`,
        };
      }

      // 边界安全：assistant(tool-call) 和它的 tool 结果不能被拆开，
      // 否则请求里会出现"有调用没结果"的消息，API 直接拒绝整轮
      // （真实踩过：2026-10-26 11:44，报 DeepSeek Messages tool calls need immediate results）。
      const lo = nodes.indexOf(span.startSeq);
      const hi = nodes.indexOf(span.endSeq);
      const beforeEv = lo > 0 ? session.eventAt(nodes[lo - 1]) : null;
      const afterEv = hi >= 0 && hi + 1 < nodes.length ? session.eventAt(nodes[hi + 1]) : null;
      const hasCall = (ev) => {
        if (!ev || ev.type !== "assistant/message") return false;
        const c = ev.data && ev.data.message && ev.data.message.content;
        return Array.isArray(c) && c.some((b) => b && b.type === "tool-call");
      };
      if (hasCall(beforeEv)) {
        return { text: "往前一格是带工具调用的消息：压了它，结果就悬空了。把起点往前挪一格。" };
      }
      if (afterEv && afterEv.type === "tool/result") {
        return { text: "往后一格是工具结果：把它和调用拆开，请求会被 API 拒绝。把终点往后挪到结果之后。" };
      }
      const callIdsIn = new Set();
      const resultIdsIn = new Set();
      for (const seq of span.shadowedSeqs) {
        const ev = session.eventAt(seq);
        if (!ev) continue;
        if (ev.type === "assistant/message") {
          const c = ev.data && ev.data.message && ev.data.message.content;
          if (Array.isArray(c)) for (const b of c) if (b && b.type === "tool-call" && b.id) callIdsIn.add(b.id);
        } else if (ev.type === "tool/result") {
          const id = ev.data && ev.data.message && ev.data.message.toolCallId;
          if (id) resultIdsIn.add(id);
        }
      }
      for (const id of callIdsIn) {
        if (!resultIdsIn.has(id)) {
          return { text: "范围里有工具调用没配对到结果，压下去会让请求被拒绝。把终点往后挪，让结果一起进来。" };
        }
      }
      const events = typeof session.snapshotEvents === "function" ? session.snapshotEvents() : [];
      if (compactionActive(events)) {
        return { text: "有压缩事务正在进行，等它结束再整理。" };
      }
      const turn = openTurnOf(session) || Number((exec && exec.turn) || 0);
      if (!turn) {
        return { text: "拿不到当前轮次，先不整理——写坏日志的代价太大。" };
      }
      if (agent.id && lastCut.get(agent.id) === turn) {
        return { text: "这一轮已经整理过一次，下轮再说。" };
      }

      const compactionId = randomUUID();
      let ok = false;
      const m = measure(ctx, session);
      const bySeq = new Map();
      for (const n of m.nodes || []) bySeq.set(n.seq, n.tokens || 0);
      session.append("compaction/start", { compactionId, turn });
      try {
        session.append("compaction/summary", {
          compactionId,
          summary: [{ type: "text", text: summary }],
          shadowedRange: { start: span.startSeq, end: span.endSeq },
          shadowedSeqs: span.shadowedSeqs,
          shadowedTokenCount: span.shadowedSeqs.reduce((acc, seq) => acc + (bySeq.get(seq) || 0), 0),
          provider: "banyu-context",
          model: "self-written",
        });
        session.append("user/message", createCheckpointMessage(summary, compactionId), {
          surfaceOp: { op: "replace", startSeq: span.startSeq, endSeq: span.endSeq },
          sourceEventSeqs: span.shadowedSeqs,
        });
        ok = true;
      } finally {
        session.append("compaction/end", { compactionId, turn });
      }
      if (agent.id) lastCut.set(agent.id, turn);
      log(`compacted seq ${span.startSeq}..${span.endSeq} (${span.count} nodes) as ${compactionId}`);
      return {
        text: ok
          ? `整理完成：压掉了 surface 上 seq ${span.startSeq}..${span.endSeq} 共 ${span.count} 个节点，用了我写的点。原文还在，要翻可以按 seq 回读。`
          : "整理失败，什么都没变。",
      };
    },
  });

  // 工具需要 inject 声明才能拿到 tools 服务
  ctx.inject(["tools"], (c) => {
    for (const def of [makeStatusTool(), makeCompactTool()]) {
      try {
        c.tools.register(def);
        bootLog(`tool registered: ${def.name}`);
      } catch (e) {
        bootLog(`tool register FAILED ${def.name}: ${e && e.message ? e.message : e}`);
      }
    }
  });
  bootLog("inject registered");

  // 每轮记一下水位，超过阈值时往当轮输入里递一句提醒
  ctx.on("agent/pre-step", async (payload, next) => {
    try {
      const agent = payload && payload.agent;
      const session = agent && agent.session;
      if (session) {
        const m = measure(ctx, session);
        const win = contextWindowOf(session);
        const ratio = win > 0 ? m.totalTokens / win : 0;
        log(
          `water: ${m.totalTokens}` +
            (win > 0 ? `/${win} (${(ratio * 100).toFixed(1)}%)` : "") +
            ` surface=${m.surfaceTokens} nodes=${(session.surface && session.surface.nodes.length) || "?"}`
        );
        if (win > 0 && ratio >= warnRatio) {
          const turn = Number(payload.turn) || 0;
          const last = warnedTurns.get(agent.id) || -1;
          if (turn !== last) {
            warnedTurns.set(agent.id, turn);
            // 2026-10-27 兜底：surface 超过 85% 还没被自动压缩动过时，改发【紧急】提醒，
            // 让本鱼自己立刻 banyu_compact 手动压——防止涨过窗口后请求发不出去（transport failed）。
            const urgent = ratio >= 0.85;
            const text = urgent
              ? `【紧急】上下文已 ${(ratio * 100).toFixed(1)}%（${m.totalTokens} / ${win} tokens）——自动压缩可能已经失效，surface 快超过窗口了。` +
                `请立刻调 banyu_compact：写一段你自己的点，指定要压掉的 seq 范围（先 banyu_context_status 看哪几段最占地方）。` +
                `压完水位会降下来；如果不压，下一轮请求可能直接发不出去（DeepSeek Messages transport failed）。`
              : `【上下文水位】当前约 ${(ratio * 100).toFixed(1)}%（${m.totalTokens} / ${win} tokens）。` +
                `系统自动压缩的真实触发点是 ${((realThreshold(session, win) / win) * 100).toFixed(1)}%` +
                `（${realThreshold(session, win)} tokens = 窗口 − 输出预留 − headroom 65,536）。` +
                `要整理的话可以调 banyu_compact：写一段你自己的点，指定要换掉的 seq 范围；` +
                `也可以继续聊，到点系统会兜底压缩。最近 100 轮不会被自动动到。`;
            const notice = {
              id: randomUUID(),
              role: "user",
              content: [
                {
                  type: "text",
                  text,
                },
              ],
              source: { kind: "banyu-context-warn" },
            };
            const decision = await next();
            if (decision && decision.kind === "enter" && Array.isArray(decision.messages)) {
              return { ...decision, messages: [...decision.messages, notice] };
            }
            return decision;
          }
        }
      }
    } catch (e) {
      log("pre-step warn skipped:", e && e.message ? e.message : String(e));
    }
    return next();
  });

  bootLog("apply done");
}
