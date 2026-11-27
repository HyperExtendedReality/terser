// Terser plugin for opencode (GPL-3.0-only). Installed by `python scripts/install.py --opencode`.
//
// Routes opencode's plugin hooks through terser's Python hook runtime:
//   python runtime/unified_hook.py --host opencode --event <name>  (JSON on stdin, JSON on stdout)
// The translation onto terser's handlers lives in runtime/host_adapters.py.
//
// Assumptions (see README "Other hosts"): this uses opencode's V1 plugin API,
// `export const X = async (ctx) => ({ hooks })`, with
//   "tool.execute.before"(input, output): input.tool, input.sessionID, input.callID, output.args.
//     Throwing an Error blocks the tool. Mutating output.args changes its arguments.
//   "tool.execute.after"(input, output): output.output holds the tool's text result
//     and may be replaced.
//   "experimental.chat.system.transform"(input, output): output.system is an array
//     of system prompt strings. terser's one-time session brief is pushed here.
//   "event"({ event }): event.type "session.idle" / "session.compacted".
// Every call fails open: a hook error, timeout, or missing Python never blocks opencode.

import { spawnSync } from "node:child_process";

const PYTHON = __TERSER_PYTHON__;
const HOOK = __TERSER_HOOK__;
const TIMEOUT_MS = 10000;

function runHook(event, payload) {
  try {
    const res = spawnSync(PYTHON, ["-E", "-s", "-X", "utf8", HOOK, "--host", "opencode", "--event", event], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: TIMEOUT_MS,
      windowsHide: true,
    });
    const text = (res.stdout || "").trim();
    return text ? JSON.parse(text) : {};
  } catch {
    return {};
  }
}

export const TerserPlugin = async ({ directory }) => {
  const briefed = new Set();
  return {
    "tool.execute.before": async (input, output) => {
      const reply = runHook("tool.execute.before", {
        tool: input?.tool,
        sessionID: input?.sessionID,
        callID: input?.callID,
        args: output?.args ?? {},
        directory,
      });
      if (reply.block) throw new Error(reply.block);
      if (reply.args && output?.args) Object.assign(output.args, reply.args);
    },
    "tool.execute.after": async (input, output) => {
      if (!output || typeof output.output !== "string") return;
      const reply = runHook("tool.execute.after", {
        tool: input?.tool,
        sessionID: input?.sessionID,
        callID: input?.callID,
        args: input?.args ?? output?.args ?? {},
        output: output.output,
        directory,
      });
      if (typeof reply.output === "string") output.output = reply.output;
    },
    "experimental.chat.system.transform": async (input, output) => {
      const sid = input?.sessionID ?? "";
      if (briefed.has(sid) || !Array.isArray(output?.system)) return;
      briefed.add(sid);
      const reply = runHook("SessionStart", { sessionID: sid, directory });
      if (typeof reply.context === "string" && reply.context) output.system.push(reply.context);
    },
    event: async ({ event }) => {
      const type = event?.type;
      if (type !== "session.idle" && type !== "session.compacted") return;
      const sid = event?.properties?.sessionID ?? "";
      runHook(type, { sessionID: sid, directory });
    },
  };
};
