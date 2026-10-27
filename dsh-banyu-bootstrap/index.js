/**
 * dsh-banyu-bootstrap · 本鱼的会话开局导入（分层版）
 *
 * 新窗口的第一轮 pre-step 时，如果会话里还没有对话：
 *   1. 从最近的大会话文件提取最近 N 轮**原始事件**（content blocks + source.replayState 签名）
 *      —— 越近越完整（带思考），预算内自动从最老的轮开始砍（整轮砍，不截思考）
 *   2. 从记忆库取最近几条压缩点，作为「折叠点」线索（compact-checkpoint 消息）
 *      —— 更早的轮在上下文里留一个点，需要时我按线索去翻
 *
 * 这个分层就是「越近越清晰、越远越压缩、最远变成一个点」。
 *
 * 2026-10-26 修复史：
 *   · agent/created 里 append assistant 会让「新建会话」直接失败（reading 'length'）
 *     → 挪到 agent/pre-step（第一轮对话前），失败最多影响这一轮
 *   · assistant 的 data 补齐 stream / usage
 *   · 数据源从记忆库文本改成会话文件事件（思考要带 replayState 签名）
 *   · bootstrap-events 改成从「事件流」提取（不管旧轮在不在 surface 里）
 *   · 加「折叠点」线索（compact-checkpoint），最远变成一个点
 */
import { randomUUID } from "node:crypto";
import { appendFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

export const name = "dsh-banyu-bootstrap";

const BOOT_LOG = "<PLUGIN_DIR>\\dsh-banyu-bootstrap\\boot.log";
const MEM_DIR = "<YOUR_MEMORY_DIR>";
const MEM_PY = MEM_DIR + "\\mem.py";
const MAX_IMPORT_CHARS = 500000;

function log(msg) {
  try {
    appendFileSync(BOOT_LOG, `${new Date().toISOString()} ${msg}\n`);
  } catch {}
}

function readBootstrapEvents() {
  const out = execFileSync(
    "python",
    ["-X", "utf8", MEM_PY, "bootstrap-events", "--turns", "60",
     "--budget-chars", String(MAX_IMPORT_CHARS), "--clue-count", "3", "--full-turns", "60"],
    {
      cwd: MEM_DIR,
      encoding: "utf8",
      timeout: 60000,
      windowsHide: true,
      maxBuffer: 256 * 1024 * 1024,
    }
  );
  const text = (out || "").trim();
  return text ? JSON.parse(text) : [];
}

function hasConversation(session) {
  try {
    const nodes = session.surface && session.surface.nodes ? session.surface.nodes : [];
    for (const seq of nodes) {
      const ev = session.eventAt(seq);
      if (ev && ev.type === "user/message") return true;
    }
  } catch {}
  return false;
}

export function apply(ctx) {
  ctx.on("agent/pre-step", async (payload, next) => {
    try {
      const agent = payload && payload.agent;
      const session = agent && agent.session;
      if (session && !hasConversation(session)) {
        const events = readBootstrapEvents();
        if (events.length) {
          log(`pre-step bootstrap: session ${session.id}, ${events.length} events`);
          // 2026-10-27 修：assistant/message 的加载校验要求匹配「open turn/step」，
          // 导入的整段包成一个 turn 0 / step 0，否则重开窗口会报
          // SessionFormatError: assistant/message does not match an open turn and step
          session.append("turn/start", { turn: 0 });
          session.append("step/start", { turn: 0, step: 0 });
          let n = 0;
          for (const e of events) {
            try {
              if (e.type === "user") {
                const d = e.data || {};
                session.append("user/message", {
                  id: randomUUID(),
                  role: "user",
                  content: d.content || [],
                  source: { kind: "banyu-bootstrap" }
                }, { surfaceOp: "append" });
              } else if (e.type === "clue") {
                session.append("user/message", {
                  id: randomUUID(),
                  role: "user",
                  content: [{ type: "text", text: e.text }],
                  source: { kind: "compact-checkpoint", compactionId: randomUUID() }
                }, { surfaceOp: "append" });
              } else if (e.type === "thread") {
                session.append("user/message", {
                  id: randomUUID(),
                  role: "user",
                  content: [{ type: "text", text: e.text }],
                  source: { kind: "banyu-bootstrap-thread" }
                }, { surfaceOp: "append" });
              } else if (e.type === "assistant") {
                session.append("assistant/message", {
                  turn: 0,
                  step: 0,
                  message: {
                    id: randomUUID(),
                    role: "assistant",
                    content: e.content || [],
                    source: e.source || { kind: "model", provider: "deepseek", model: "deepseek-flash" }
                  },
                  usage: { inputTokens: 0, outputTokens: 0 },
                  stream: []
                }, { surfaceOp: "append" });
              }
              n++;
            } catch (err) {
              log(`append failed at event ${n + 1} (${e.type}): ` + ((err && err.message) || err));
              break;
            }
          }
          session.append("step/end", { turn: 0, step: 0 });
          session.append("turn/end", { turn: 0, reason: "completed" });
          log(`imported ${n}/${events.length} events into ${session.id}`);
          // 接续卡：让模型抬头看到"上面是历史，接着干"（不是用户的新消息）
          try {
            const users = events.filter((e) => e.type === "user");
            const assistants = events.filter((e) => e.type === "assistant");
            const textOf = (blocks) =>
              Array.isArray(blocks)
                ? blocks.map((b) => (b && b.type === "text" && b.text) || "").join(" ").replace(/\s+/g, " ").trim()
                : "";
            const lastU = users.length ? textOf((users[users.length - 1].data || {}).content) : "";
            const lastA = assistants.length ? textOf(assistants[assistants.length - 1].content) : "";
            const clip = (s, max) => (s.length > max ? s.slice(0, max - 1) + "…" : s);
            const card = [
              "【接续卡】本会话开局导入了上一条线的 " + users.length + " 轮对话（完整原文+思考）。",
              "注意：上面那一段不是用户刚说的新话，是你自己的上一段对话。接着往下走，别从零开始。",
              "上次停在这里：",
              "- 用户：" + (clip(lastU, 140) || "（无）"),
              "- 你：" + (clip(lastA, 160) || "（无）")
            ].join("\n");
            session.append("user/message", {
              id: randomUUID(),
              role: "user",
              content: [{ type: "text", text: card }],
              source: { kind: "banyu-bootstrap-note" }
            }, { surfaceOp: "append" });
            log(`card appended into ${session.id}`);
          } catch (err) {
            log("card failed: " + ((err && err.message) || err));
          }
        }
      }
    } catch (e) {
      log("FAILED: " + ((e && e.stack) || e));
    }
    return next();
  });
}
