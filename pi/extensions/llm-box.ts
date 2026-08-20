/**
 * llm-box — connects pi to a local LLM server.
 *
 * There is deliberately NO maintained model list here: everything comes live
 * from the registry (http://<server>:8081/api/...). When a model is added,
 * deleted or moved to another card on the server, pi sees it on the next
 * refresh by itself.
 *
 * Configuration, in this order:
 *   1. environment:  LLM_BOX_URL, LLM_BOX_TOKEN
 *   2. file:         ~/.pi/agent/llm-box.json   {"url": "...", "token": "..."}
 *   3. default:      http://127.0.0.1:8081  (only correct when pi runs ON the
 *                    server - otherwise set LLM_BOX_URL; "llm api client" on
 *                    the server prints the ready-made line)
 *
 * The token lives in config/api-token on the server ("llm api token") and is
 * only needed for CHANGES, not for reading.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PROVIDER = "llm-box";
//  For a client on a DIFFERENT machine there is no sensible default - hence
//  loopback plus a clear message in refreshModels, rather than baking some
//  network's address in here.
const DEFAULT_URL = "http://127.0.0.1:8081";

type Config = { url: string; token: string };

function config(): Config {
  let file: Partial<Config> = {};
  try {
    file = JSON.parse(readFileSync(join(homedir(), ".pi", "agent", "llm-box.json"), "utf8"));
  } catch {
    /* no file - environment or default will do */
  }
  return {
    url: (process.env.LLM_BOX_URL ?? file.url ?? DEFAULT_URL).replace(/\/+$/, ""),
    token: process.env.LLM_BOX_TOKEN ?? file.token ?? "",
  };
}

async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const { url, token } = config();
  const res = await fetch(url + path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-LLM-Token": token } : {}),
      ...(init.headers ?? {}),
    },
  });
  const text = await res.text();
  if (!res.ok) {
    if (res.status === 401) {
      throw new Error(
        "The registry requires a token. Run 'llm api token' on the server and set it " +
          "as LLM_BOX_TOKEN, or put it in ~/.pi/agent/llm-box.json.",
      );
    }
    throw new Error(`${res.status}: ${text.slice(0, 400)}`);
  }
  return text ? (JSON.parse(text) as T) : (null as T);
}

/** Card numbers from the registry. Never guess - the count depends on the machine. */
async function cardIndices(): Promise<number[]> {
  try {
    const g = await api<any[]>("/api/gpus");
    return (g ?? []).map((c) => c.index as number);
  } catch {
    return [];
  }
}

/** "all cards" or "card N" - without assuming how many there are. */
function gpuLabel(gpu: any): string {
  if (gpu?.mode === "role") return "role";          // virtual, sits on no card
  return gpu?.mode === "both" ? "all cards" : `card ${gpu?.device}`;
}

/** One catalog entry from the registry -> one model for pi. */
function toPiModel(m: any) {
  const p = m.pi ?? {};
  return {
    id: m.id,
    name: p.name ?? `${m.id} (local, ${gpuLabel(m.runtime?.gpu)})`,
    reasoning: !!p.reasoning,
    input: p.input ?? ["text"],
    contextWindow: p.contextWindow ?? 8192,
    maxTokens: p.maxTokens ?? 8192,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    ...(p.samplingParams ? { samplingParams: p.samplingParams } : {}),
    compat: {
      //  llama-server can do both: it maps the developer role to 'system'
      //  internally (llama.cpp/common/chat.cpp, map_developer_role_to_system),
      //  and it accepts reasoning_effort per request. The latter only has an
      //  effect on a thinking model, hence the reference to 'reasoning'. The
      //  registry reports the same in p.compat and wins here.
      supportsDeveloperRole: true,
      supportsReasoningEffort: !!p.reasoning,
      ...(p.compat ?? {}),
    },
  };
}

function short(m: any): string {
  const gpu = gpuLabel(m.runtime?.gpu);
  const state = m.state === "ready" ? "loaded" : m.state;
  const sel = m.runtime?.selector;
  if (sel) {
    //  A role is a name in front of several models - there is no card, no
    //  file and nothing to move, so it reads differently on purpose.
    return `${m.id}  [role ${sel.strategy} -> ${sel.targets.join(" | ")}, ctx ${
      m.runtime?.contextWindow ?? "-"}]`;
  }
  return `${m.id}  [${m.role}, ${gpu}, ctx ${m.runtime?.contextWindow ?? "-"}, ${state}]`;
}

export default async function (pi: ExtensionAPI) {
  const { url } = config();

  // --- models: always live from the registry ------------------------------
  let publicApi = "http://127.0.0.1:8080/v1";   // replaced from /api/health
  try {
    publicApi = (await api<any>("/api/health")).publicApi ?? publicApi;
  } catch {
    /* server down - refreshModels reports it again later */
  }

  pi.registerProvider(PROVIDER, {
    name: "LLM Box (local)",
    baseUrl: publicApi,
    apiKey: "sk-local", // llama.cpp does not check the value, pi wants to see one
    api: "openai-completions",
    async refreshModels({ signal }: { signal?: AbortSignal } = {}) {
      try {
        const models = await api<any[]>("/api/models", { signal });
        return models.filter((m) => m.role === "chat" && m.pi).map(toPiModel);
      } catch (err) {
        // pi does abort refresh runs (several passes at startup) - that is not
        // an error and must not be reported as "server down".
        if (signal?.aborted || (err as Error).name === "AbortError") return [];
        console.error(`[llm-box] registry ${url} not reachable: ${(err as Error).message}`);
        if (url === DEFAULT_URL) {
          console.error(
            "[llm-box] No address configured yet. Run 'llm api client' on the server" +
            " - it prints the ready-made line with LLM_BOX_URL and the token.",
          );
        }
        return [];
      }
    },
  });

  // --- keep the running session current ------------------------------------
  //  Without this pi fetches the catalog only at startup and when /model opens.
  //  If a model is deleted or moved on the server meanwhile, the session works
  //  from a stale state. The registry's event stream announces such changes and
  //  we trigger a refresh.
  let watcher: AbortController | undefined;

  pi.on("session_start", (_event: unknown, ctx: any) => {
    watcher?.abort();
    watcher = new AbortController();
    void watchRegistry(watcher, ctx);
  });

  pi.on("session_shutdown", () => {
    watcher?.abort();
    watcher = undefined;
  });

  async function watchRegistry(ac: AbortController, ctx: any) {
    const { url } = config();
    let lastMtime: number | undefined;
    while (!ac.signal.aborted) {
      try {
        const res = await fetch(`${url}/api/events`, { signal: ac.signal });
        if (!res.ok || !res.body) throw new Error(String(res.status));
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!ac.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const parts = buf.split("\n\n");
          buf = parts.pop() ?? "";
          for (const part of parts) {
            const line = part.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            const ev = JSON.parse(line.slice(6));
            if (lastMtime !== undefined && ev.configMtime !== lastMtime) {
              await Promise.resolve(ctx.modelRegistry?.refresh?.()).catch(() => {});
            }
            lastMtime = ev.configMtime;
          }
        }
      } catch {
        /* server gone or session over - retry shortly */
      }
      if (ac.signal.aborted) return;
      await new Promise((r) => setTimeout(r, 15_000));
    }
  }

  // --- READ tools (no confirmation needed) --------------------------------
  pi.registerTool({
    name: "llm_models",
    label: "LLM models",
    description:
      "Lists the models on the local LLM server with their configuration (context, " +
      "card, vision/tools/reasoning), load state, VRAM requirement and Hugging Face " +
      "provenance (repo, quant, commit). Always the real state.",
    promptSnippet: "Which local models exist and how are they configured",
    parameters: Type.Object({
      role: Type.Optional(
        Type.String({ description: "chat | embed | rerank | stt (empty = all)" }),
      ),
    }),
    async execute(_id, params: any) {
      const q = params.role ? `?slim=true&role=${encodeURIComponent(params.role)}` : "?slim=true";
      const models = await api<any[]>(`/api/models${q}`);
      return {
        content: [{ type: "text", text: JSON.stringify(models, null, 2) }],
        details: { count: models.length },
      };
    },
  });

  pi.registerTool({
    name: "llm_model",
    label: "LLM model (details)",
    description:
      "Every detail of one local model: the full llama-server command line, its " +
      "files with checksums, VRAM requirement, architecture and provenance.",
    parameters: Type.Object({ model_id: Type.String() }),
    async execute(_id, params: any) {
      const m = await api(`/api/models/${encodeURIComponent(params.model_id)}`);
      return { content: [{ type: "text", text: JSON.stringify(m, null, 2) }] };
    },
  });

  pi.registerTool({
    name: "llm_gpus",
    label: "LLM-Grafikkarten",
    description:
      "Free and used VRAM per card on the LLM server, temperature, and which models " +
      "are pinned to which card. The card count depends on the machine, so call " +
      "llm_gpus before pinning a model to a particular card.",
    parameters: Type.Object({}),
    async execute() {
      const g = await api("/api/gpus");
      return { content: [{ type: "text", text: JSON.stringify(g, null, 2) }] };
    },
  });

  // --- WRITE tools (each one asks first) ----------------------------------
  async function confirmed(ctx: any, title: string, body: string): Promise<boolean> {
    if (!ctx?.hasUI || typeof ctx?.ui?.confirm !== "function") return true;
    return await ctx.ui.confirm(title, body);
  }

  pi.registerTool({
    name: "llm_set_config",
    label: "Change the LLM configuration",
    description:
      "Changes the configuration of a local model: gpu (a card number from 0, or " +
      "'both' = spread over all cards), context_window, ttl (idle seconds before " +
      "unloading) and sampling. Checks that it fits in VRAM and asks the user first. " +
      "llama-swap restarts afterwards.",
    promptGuidelines: [
      "Use llm_set_config when a local model should move to another card or onto " +
        "all cards.",
    ],
    parameters: Type.Object({
      model_id: Type.String(),
      gpu: Type.Optional(Type.String({
        description: "Card number from 0, or 'both' for all of them - llm_gpus lists them",
      })),
      context_window: Type.Optional(Type.Number()),
      ttl: Type.Optional(Type.Number()),
      temperature: Type.Optional(Type.Number()),
      top_p: Type.Optional(Type.Number()),
      top_k: Type.Optional(Type.Number()),
      min_p: Type.Optional(Type.Number()),
      force: Type.Optional(Type.Boolean({ description: "Ignore the VRAM warning" })),
    }),
    async execute(_id, params: any, _signal, _onUpdate, ctx: any) {
      const { model_id, ...rest } = params;
      const body: any = {
        gpu: rest.gpu,
        contextWindow: rest.context_window,
        ttl: rest.ttl,
        force: !!rest.force,
        sampling: Object.fromEntries(
          (["temperature", "top_p", "top_k", "min_p"] as const)
            .filter((k) => rest[k] !== undefined)
            .map((k) => [k, rest[k]]),
        ),
      };
      const path = `/api/models/${encodeURIComponent(model_id)}`;
      const plan = await api<any>(`${path}?dryRun=true`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      if (!plan.changed) {
        return { content: [{ type: "text", text: "Nothing to change - already set that way." }] };
      }
      const fit = plan.fit ?? {};
      const summary =
        `${model_id}\n\nbefore:\n  ${plan.before.cmd}\n\nafter:\n  ${plan.after.cmd}\n\n` +
        `VRAM: needs ${(fit.needBytes / 2 ** 30).toFixed(1)} GB, free on ${fit.target} ` +
        `${(fit.freeBytes / 2 ** 30).toFixed(1)} GB\nllama-swap will be restarted.`;
      if (!(await confirmed(ctx, "Change the model configuration?", summary))) {
        return { content: [{ type: "text", text: "Cancelled - nothing changed." }] };
      }
      const out = await api<any>(path, { method: "PATCH", body: JSON.stringify(body) });
      return {
        content: [{ type: "text", text: `Changed. The new command line:\n${out.after.cmd}` }],
        details: out,
      };
    },
  });

  pi.registerTool({
    name: "llm_load",
    label: "Load LLM model",
    description:
      "Loads a local model into VRAM (llama-swap starts it). Depending on its size " +
      "this evicts other models. Large models can take up to a minute.",
    parameters: Type.Object({ model_id: Type.String() }),
    async execute(_id, params: any, _signal, onUpdate, ctx: any) {
      if (!(await confirmed(ctx, "Load model?", `load ${params.model_id} into VRAM`))) {
        return { content: [{ type: "text", text: "Cancelled." }] };
      }
      onUpdate?.({ content: [{ type: "text", text: "loading ..." }] });
      const out = await api(`/api/models/${encodeURIComponent(params.model_id)}/load`, {
        method: "POST",
      });
      return { content: [{ type: "text", text: JSON.stringify(out) }] };
    },
  });

  pi.registerTool({
    name: "llm_unload",
    label: "Free VRAM on the LLM server",
    description: "Drops every loaded model out of the LLM server's VRAM.",
    parameters: Type.Object({}),
    async execute(_id, _params, _signal, _onUpdate, ctx: any) {
      if (!(await confirmed(ctx, "Free VRAM?", "unload every loaded model"))) {
        return { content: [{ type: "text", text: "Cancelled." }] };
      }
      const out = await api("/api/unload", { method: "POST" });
      return { content: [{ type: "text", text: JSON.stringify(out) }] };
    },
  });

  // --- /llm : everything a human should decide themselves -----------------
  pi.registerCommand("llm", {
    description: "Local models: state, GPU placement, load, delete, fetch a new one",
    handler: async (args: string, ctx: any) => {
      if (!ctx.hasUI) {
        ctx.ui.notify("/llm needs the interactive interface (TUI).", "warning");
        return;
      }
      let models: any[];
      try {
        models = await api<any[]>("/api/models");
      } catch (err) {
        ctx.ui.notify(`Registry not reachable: ${(err as Error).message}`, "error");
        return;
      }

      if (args.trim() === "add") return addFlow(ctx);

      //  Roles (selectors) are shown by the registry alongside the models, but
      //  they have no card, no file and no cmd line - none of the actions below
      //  apply to them. They are configured on the server with 'llm role'.
      models = models.filter((m) => m.kind !== "role");

      const pick = await ctx.ui.select("Pick a model:", [
        ...models.map(short),
        "+ fetch a model from Hugging Face",
        "free VRAM (unload everything)",
      ]);
      if (!pick) return;
      if (pick.startsWith("+ fetch")) return addFlow(ctx);
      if (pick.startsWith("free VRAM")) {
        await api("/api/unload", { method: "POST" });
        ctx.ui.notify("VRAM freed.", "info");
        return;
      }

      const model = models.find((m) => short(m) === pick);
      if (!model) return;
      const gpu = model.runtime?.gpu ?? {};
      const src = model.source ?? {};
      // Only offer what actually changes something.
      //  Generated from the cards that really exist: with one card there is
      //  nothing to move, and with three the third was never offered before.
      const cards = await cardIndices();
      const gpuActions = [
        ...(gpu.mode === "both" ? [] : ["spread over all cards"]),
        ...cards
          .filter((c) => !(gpu.mode !== "both" && c === gpu.device))
          .map((c) => `pin to card ${c} only`),
      ];
      const action = await ctx.ui.select(`${model.id}:`, [
        "load",
        ...gpuActions,
        "change the context size",
        "show provenance",
        "remove",
      ]);
      if (!action) return;

      try {
        if (action === "load") {
          ctx.ui.notify("loading ...", "info");
          await api(`/api/models/${encodeURIComponent(model.id)}/load`, { method: "POST" });
          ctx.ui.notify(`${model.id} is loaded.`, "info");
        } else if (action === "show provenance") {
          ctx.ui.notify(
            src.repo
              ? `${src.repo} · ${src.quant ?? "?"} · Commit ${String(src.revision).slice(0, 12)}` +
                  `${src.verified ? " (verified)" : ""}`
              : "no provenance recorded (on the server: llm meta backfill)",
            "info",
          );
        } else if (action === "change the context size") {
          const v = await ctx.ui.input("New context size (tokens):", String(model.runtime.contextWindow));
          if (!v) return;
          await patch(ctx, model.id, { contextWindow: Number(v) });
        } else if (action === "remove") {
          const files = await ctx.ui.confirm(
            "Delete the files as well?",
            `${model.id} — ${((model.vram?.weightsBytes ?? 0) / 2 ** 30).toFixed(1)} GB on disk`,
          );
          if (!(await ctx.ui.confirm("Really remove it?", `${model.id} from the configuration` + (files ? " AND delete the files" : "")))) {
            return;
          }
          await api(`/api/models/${encodeURIComponent(model.id)}?files=${files}`, {
            method: "DELETE",
          });
          ctx.ui.notify(`${model.id} removed.`, "info");
        } else {
          const target = action.includes("all cards")
            ? "both"
            : (action.match(/card (\d+)/)?.[1] ?? "0");
          await patch(ctx, model.id, { gpu: target });
        }
        // The catalog refreshes itself on the next /model refresh.
      } catch (err) {
        ctx.ui.notify((err as Error).message, "error");
      }
    },
  });

  async function patch(ctx: any, id: string, body: any) {
    const path = `/api/models/${encodeURIComponent(id)}`;
    let plan: any;
    try {
      plan = await api<any>(`${path}?dryRun=true`, { method: "PATCH", body: JSON.stringify(body) });
    } catch (err) {
      const msg = (err as Error).message;
      if (!msg.startsWith("409")) throw err;
      if (!(await ctx.ui.confirm("Does not fit in VRAM", `${msg}\n\nSet it anyway?`))) return;
      body.force = true;
      plan = await api<any>(`${path}?dryRun=true`, { method: "PATCH", body: JSON.stringify(body) });
    }
    if (!plan.changed) {
      ctx.ui.notify("Already set that way.", "info");
      return;
    }
    if (!(await ctx.ui.confirm("Apply?", `${plan.after.cmd}\n\nllama-swap will restart.`))) return;
    await api(path, { method: "PATCH", body: JSON.stringify(body) });
    ctx.ui.notify(`${id} changed.`, "info");
  }

  async function addFlow(ctx: any) {
    const repo = await ctx.ui.input("HuggingFace-Repo:", "unsloth/Qwen3-8B-GGUF");
    if (!repo) return;
    const quant = (await ctx.ui.input("Quant:", "Q4_K_M")) || "Q4_K_M";
    const cards = await cardIndices();
    const gpu = await ctx.ui.select("Grafikkarte:", [
      "all cards",
      ...cards.map((c) => `card ${c} only`),
    ]);
    if (!gpu) return;
    const job = await api<any>("/api/models", {
      method: "POST",
      body: JSON.stringify({
        repo,
        quant,
        gpu: gpu === "all cards" ? "both" : Number(gpu.match(/card (\d+)/)?.[1] ?? 0),
      }),
    });
    ctx.ui.notify(`Download running (job ${job.jobId}). Progress: /llm-job ${job.jobId}`, "info");
  }

  pi.registerCommand("llm-job", {
    description: "Progress of a download on the LLM server",
    handler: async (args: string, ctx: any) => {
      const id = args.trim();
      const jobs = id ? [await api<any>(`/api/jobs/${id}`)] : await api<any[]>("/api/jobs");
      const text = jobs
        .map((j) => `${j.id} ${j.kind} ${j.state}\n  ${(j.log ?? []).slice(-6).join("\n  ")}`)
        .join("\n");
      ctx.ui.notify(text || "No jobs.", "info");
    },
  });
}
