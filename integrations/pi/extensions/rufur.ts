/**
 * pi ↔ RUFUR extension: automatic recall and capture (the pi equivalent of the Claude Code hooks).
 *
 *   first prompt       start a RUFUR session and preload the working set (profile, recent, project facts)
 *   every prompt       capture it; recall relevant memories not already sent this session
 *   every agent run    add the working set + this session's recalled memories to the system prompt
 *   agent run ends     capture the assistant's final reply
 *   session shutdown   end the RUFUR session (extraction runs in the daemon's background)
 *
 * JSON-mode runs (`pi --mode json`, which is how pi subagents are spawned) are not captured: their prompts
 * are written by the parent agent, not the human. Subagents get memory through `rufur.py spawn` instead.
 *
 * Install: copy to ~/.pi/agent/extensions/rufur.ts. Everything fails open: if RUFUR is down, pi works normally.
 * Env: RUFUR_URL (default http://127.0.0.1:7437), RUFUR_API_KEY or RUFUR_AGENT (default "pi"),
 * RUFUR_PROMPT_BUDGET (default 300), RUFUR_CAPTURE=0 to disable.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { basename } from "node:path"

const BASE = (process.env.RUFUR_URL ?? "http://127.0.0.1:7437").replace(/\/$/, "")
const AGENT = process.env.RUFUR_AGENT ?? "pi"
const KEY = process.env.RUFUR_API_KEY
const BUDGET = Number(process.env.RUFUR_PROMPT_BUDGET ?? 300)
const MAX_CHARS = 4000
const KEEP_RECALLS = 5 // recalled blocks kept in the system prompt per session

async function call(path: string, body: unknown = {}, timeoutMs = 3000): Promise<any> {
  const headers: Record<string, string> = { "content-type": "application/json" }
  if (KEY) headers.authorization = `Bearer ${KEY}`
  else headers["x-rufur-agent"] = AGENT
  const r = await fetch(BASE + path, { method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs) })
  if (!r.ok) throw new Error(`rufur ${path}: ${r.status}`)
  return r.json()
}

async function quietly<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {
    return await fn()
  } catch {
    return undefined // memory must never break the user's session
  }
}

const textOf = (content: unknown): string =>
  typeof content === "string"
    ? content
    : Array.isArray(content)
      ? content.filter((p: any) => p?.type === "text").map((p: any) => p.text).join("\n")
      : ""

const note = (sid: string) =>
  `RUFUR memory is active (RUFUR session ${sid}). Everything the user says is captured and remembered automatically, and ` +
  `relevant memories are added to your context, so you do not need to save the user's own words. ` +
  `For more, or to save a conclusion once a task is done, resolve conflicts, delegate to subagents or forget, load the rufur-memory skill.`

export default function (pi: ExtensionAPI) {
  let rufur: string | undefined // RUFUR session id for the current pi session
  let base = "" // working set injected at session start
  let recalls: string[] = []
  const off = process.env.RUFUR_CAPTURE === "0"

  async function end() {
    if (rufur) await quietly(() => call(`/v1/sessions/${rufur}/end?background=true`, {}, 1500))
    rufur = undefined
    base = ""
    recalls = []
  }

  pi.on("before_agent_start", async (event, ctx) => {
    if (off || ctx.mode === "json") return
    const prompt = (event.prompt ?? "").trim()
    await quietly(async () => {
      if (!rufur) {
        const out = await call("/v1/bootstrap", { project: basename(ctx.cwd), prompt: prompt || undefined }, 6000)
        rufur = out.session_id as string
        process.env.RUFUR_SESSION = rufur // lets `rufur.py spawn` tie subagent pools to this session
        base = [note(rufur), out.context].filter(Boolean).join("\n\n")
      }
      if (!prompt) return
      await call("/v1/events", { content: prompt.slice(0, MAX_CHARS), role: "user", session_id: rufur })
      const r = await call(
        "/v1/recall",
        { query: prompt.slice(0, 1000), session_id: rufur, token_budget: BUDGET, verify: false, exclude_sent: true },
        2500,
      )
      if (!r.abstained && r.items?.length) recalls = [...recalls, r.context].slice(-KEEP_RECALLS)
    })
    if (!rufur) return
    return { systemPrompt: [event.systemPrompt, base, ...recalls].filter(Boolean).join("\n\n") }
  })

  pi.on("agent_end", async (event, ctx) => {
    if (off || ctx.mode === "json" || !rufur) return
    const last = [...(event.messages ?? [])].reverse().find((m: any) => m.role === "assistant")
    const text = textOf(last?.content).trim()
    if (text) await quietly(() => call("/v1/events", { content: text.slice(0, MAX_CHARS), role: "assistant", session_id: rufur }))
  })

  // /new, /resume, /fork and quitting all end the current pi session.
  pi.on("session_shutdown", async () => {
    await end()
  })
}
