/**
 * dsh-banyu-expand · 让「点」能撑开
 *
 * DSH 的压缩是原地替换：旧消息被摘要节点盖住，原文还在会话日志里
 * （compaction/summary 的 shadowedSeqs 记着每一个被盖住的 seq）。
 * 这个插件提供一个工具 banyu_expand：按 seq 号把原文读回来。
 *
 * 设计取舍：
 *   · 只读会话文件，绝不写
 *   · 找不到就如实返回错误，不瞎猜
 *   · 不依赖 DSH 内部包，纯 JS + python 解压
 */
import { execFileSync } from "node:child_process";

export const name = "dsh-banyu-expand";

const PYTHON =
  "<YOUR_HOME>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe";
const SCRIPT = "<PLUGIN_DIR>\\dsh-banyu-expand\\expand.py";

export function apply(ctx) {
  try {
    ctx.inject(["tools"], (c) => {
      try {
        c.tools.register({
          name: "banyu_expand",
          description:
            "展开一个压缩点：按 seq 号把被压缩盖住的原文读回来。DSH 会话日志里原文永远在，只是被摘要盖住了。当你想确认「那次到底说了什么」、或者摘要里的锚写着 seq 范围时用它。",
          parameters: {
            type: "object",
            properties: {
              seq_start: {
                type: "number",
                description: "起点 seq（从压缩点的锚里读）",
              },
              seq_end: {
                type: "number",
                description: "终点 seq；省略时只读起点之后几条",
              },
              session: {
                type: "string",
                description:
                  "会话 id 前缀，例如 3bc14c68；省略时用最近修改的会话（通常就是当前会话）",
              },
            },
            required: ["seq_start"],
          },
          output: {
            schema: {
              type: "object",
              properties: {
                text: { type: "string" },
                error: { type: "string" },
              },
              additionalProperties: false,
            },
            render: (_args, value) => [
              {
                type: "text",
                text: String((value && (value.text || value.error)) || ""),
              },
            ],
          },
          execute: async (args) => {
            const start = Number((args && args.seq_start) || 0);
            const end =
              args && args.seq_end ? Number(args.seq_end) : start + 8;
            const sess = String((args && args.session) || "");
            if (!Number.isFinite(start)) {
              return { error: "seq_start 必须是数字" };
            }
            try {
              const out = execFileSync(
                PYTHON,
                ["-X", "utf8", SCRIPT, sess, String(start), String(end)],
                { encoding: "utf8", timeout: 30000, windowsHide: true }
              );
              return { text: String(out || "").trim().slice(0, 8000) };
            } catch (e) {
              return {
                error: "展开失败：" + String((e && e.message) || e),
              };
            }
          },
        });
      } catch (e) {
        console.warn(
          "[banyu-expand] 注册工具失败：",
          e && e.message ? e.message : e
        );
      }
    });
  } catch (e) {
    console.warn("[banyu-expand] inject 失败：", e && e.message ? e.message : e);
  }
}
