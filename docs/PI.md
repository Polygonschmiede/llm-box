# Connecting the pi agent

The goal: **never maintain a `models.json` again.** pi fetches models and their
configuration live from the registry on every refresh (see [API.md](API.md)), and
through a set of tools it can change things too — moving a model from one card to all
of them, for instance.

## Setting up a client machine

This applies to any machine pi runs on — macOS, Linux, whatever. Afterwards nothing
changes there again, not even when models appear, disappear or move between cards on
the server.

A single command **on the server** prints everything with the real values:

```bash
llm api client
```

### 1. On the server: port 8081 has to be reachable

If a firewall is active, the port has to be open for your network — otherwise nothing
arrives from the client and it fails *silently* (the service runs, the log just says
nothing):

```bash
sudo ufw allow from <your-subnet> to any port 8081 proto tcp comment 'llm registry'
```

`setup-system.sh` does this for you when `LLM_BIND=0.0.0.0` is used. Note that the
services ship bound to loopback, so this step also means deciding to open them — see
[../SECURITY.md](../SECURITY.md).

### 2. Check reachability — before involving pi

From the client machine:

```bash
curl -s http://<server-ip>:8081/api/health
```

Expected: `{"ok":true,"swapUp":true,"models":10,…}`. If nothing comes back it is the
network or the firewall, and pi cannot do anything about that.

### 3. Install the extension

```bash
pi install https://github.com/Polygonschmiede/llm-box
```

Then tell it where the server is — there is no useful default for a client on a
different machine:

```bash
export LLM_BOX_URL=http://<server-ip>:8081
```

or `{"url": "…"}` in `~/.pi/agent/llm-box.json`.

### 4. Only for making changes: the token

Switching cards, changing context, loading — that needs the token from
`llm api token`:

```bash
echo '{"url": "http://<server-ip>:8081", "token": "…"}' > ~/.pi/agent/llm-box.json
```

Or `export LLM_BOX_TOKEN=…`. Without a token you still see every model and work with
them normally; only changing configuration is refused.

### 5. Remove leftovers

If you previously configured the server by hand, two remnants keep making themselves
felt:

```bash
mv ~/.pi/agent/models.json ~/.pi/agent/models.json.old   # old provider, frozen list
```

And `~/.pi/agent/settings.json` may still list old model names under `enabledModels`.
Those show up at startup as `Warning: No models match pattern …` and as
`[unavailable]` in the `/model` dialog. **Deleting the `enabledModels` key entirely is
best** — then every available model is usable. To restrict the choice, use a pattern
instead of individual names, because a pattern does not go stale:

```json
{ "enabledModels": ["llm-box/*"] }
```

### 6. Verify

```bash
pi --list-models
```

Expected: only `llm-box`, with all the server's chat models and their real context
sizes. No warnings, no second provider.

### Staying current

The catalog updates itself — the **extension** does not. If pi reports "Package
Updates Available" at startup, one command fixes it:

```bash
pi update --extensions
```

## What can go wrong

| Symptom | Cause and fix |
|---|---|
| `fetch failed` on `llm_models`, while `curl` to 8080 works | port 8081 is not open (step 1). A test *on the server* goes over `lo` and proves nothing. |
| No `llm-box` models, "registry not reachable" | server down, wrong `LLM_BOX_URL`, or step 1 missing. The extension prints the `llm api client` hint when no address was set at all. |
| `Warning: No models match pattern …`, `[unavailable]` in `/model` | stale entries in `enabledModels` (step 5). |
| Models appear twice, or long-deleted ones show up | the old `models.json` is still there (step 5). |
| Changes are refused, asking for a token | step 4; get it with `llm api token` on the server. |
| `Too many authentication failures` when SSHing to the server | the agent offers too many keys. Use `ssh -o IdentitiesOnly=yes -i ~/.ssh/<key> …` or a host entry in `~/.ssh/config`. |

**The session stays current:** the extension listens on the registry's event stream
(`/api/events`). When something changes on the server — a model deleted, moved to
another card, context changed — pi re-requests the catalog by itself. You do not have
to restart or even open `/model`. If the connection drops it is retried every 15 s. If
the server is off, the extension says so once and pi starts anyway.

## What the agent knows and can do

**Reading — no confirmation, any time:**

| Tool | Answer |
|---|---|
| `llm_models` | every model: role, context, card, vision/tools/reasoning, load state, size, VRAM need, **Hugging Face provenance** |
| `llm_model` | one model in detail: the full llama-server line, files with checksums, architecture |
| `llm_gpus` | free VRAM per card, temperature, what is pinned where |

**Changing — every action asks first, in the pi window:**

| Tool | Effect |
|---|---|
| `llm_set_config` | `gpu` (a card number or `both`), `context_window`, `ttl`, sampling |
| `llm_load` | pull a model into VRAM |
| `llm_unload` | free the VRAM |

A dry run always happens first: the confirmation shows the old and the new command
line and what it means for VRAM. If it does not fit on the target card, pi says so and
offers to force it anyway.

Questions that now simply work:

> "Which models do you have locally, and which card is each on?"
> "Put the 27B across all cards, I need more context."
> "Who published the coder model, and which quant is it?"

**Deleting and downloading are not done by the agent** — those stay with you.

## `/llm` — the things you decide yourself

```
/llm            pick a model → load · switch card · change context
                            · show provenance · delete · fetch a new model
/llm add        straight to the download dialog
/llm-job <id>   progress of a running download
```

The card menu is built from the cards the server actually reports, so a third card
is offered and a single-card machine has nothing to "move". Deleting asks twice:
first whether the files should go too, then once more for safety.

## What is derived per model

The registry works out what pi needs from the `cmd` line and the GGUF header:
`contextWindow` from `-c`, `maxTokens` as `min(ctx, 32768)`, `input` from `--mmproj`,
`reasoning` from `-rea off` or the model name, `samplingParams` from
`--temp/--top-p/…`, and `compat.thinkingFormat` = `qwen-chat-template` for thinking
Qwen3 models.

Two capability flags are reported rather than guessed:

- `supportsDeveloperRole: true` — llama.cpp maps the `developer` role to `system`
  internally, so it works for every model.
- `supportsReasoningEffort` — **per model**, taken from whether the model reasons at
  all. llama-server accepts the OpenAI `reasoning_effort` field per request and passes
  it to the chat template; it only has an effect on a thinking model. This used to be
  hardcoded to `false` for everything, which meant the effort level always fell back
  to the template default (`xhigh` on Qwen3.8) and every answer thought at maximum
  length.

Embedders, rerankers and Whisper are not reported to pi: they hang off the same
endpoint but are not chat models.

## Giving subagents a different model

The registry serves **roles** (`llm role` on the server) alongside the models, so
they show up in pi's model list like any other entry. A role is one name in front
of several models, and the server decides which one answers:

```bash
# on the server
llm role set coder spillover qwen3.8-27b-q6_k <small-model> --spillover=2
llm role set fast  pin       <small-model>
llm restart
```

Then point the main agent at `coder` and its subagents at `fast`. With `spillover`
you do not even have to split them by hand: the first two concurrent requests go to
the big model on card 0, and everything beyond that starts the small model on card 1.
The client sends `coder` either way.

Two things to know before you rely on it:

- A role reports the **smallest** context window and the **intersection** of the
  capabilities of its targets — otherwise pi would send 131k tokens at a model that
  holds 8k. `llm role` prints the effective values; check them after changing
  targets, because adding one small model shrinks the whole role.
- Roles are read-only from pi's side. The tools that move a model between cards or
  change its context (`llm_set_config`, `/llm`) deliberately skip them: a role has
  no card, no file and no command line. Change one with `llm role` on the server.

## When the derivation is wrong

One line **inside the model's marker block** in `config/llama-swap.yaml` (`llm edit`)
always wins:

| Line | Effect |
|---|---|
| `# pi: skip` | do not report this model to pi (useful for small service models) |
| `# pi: name=…` | display name / match target for `pi --model <pattern>` |
| `# pi: reasoning=false` | override the thinking-model detection |
| `# pi: input=text,image` | force vision on or off |
| `# pi: contextWindow=131072` | report a different context |
| `# pi: maxTokens=65536` | more room for the answer |
| `# pi: thinkingFormat=qwen-chat-template` | set the thinking format |
| `# pi-json: {"compat":{…}}` | mix in arbitrary fields from the pi documentation |

A trailing comment (` # …`) is allowed. The same works over the API through
`piOverrides` in `PATCH /api/models/{id}`.

> **`maxTokens` on reasoning models:** they write a thinking block first. With too
> little room you get an empty answer and `finish_reason: length` (see
> [MODELS.md](MODELS.md)). The default of 32768 is comfortable.

## Without the extension (fallback)

On a machine where the extension is not installed:

```bash
mkdir -p ~/.pi/agent
curl -s http://<server-ip>:8081/api/pi-models.json > ~/.pi/agent/models.json
```

That is a snapshot — it goes stale the moment anything changes on the server. Which is
exactly why the extension exists.

## Why not pi's built-in llama.cpp mode?

pi can talk to the **llama.cpp router** (`llama-server --models-dir …`) directly via
`/login llama.cpp` and `/llama`, and load and unload models there. That is a different
server from **llama-swap** and cannot do what is needed here: per-model GPU pinning
(`--device ROCmN` plus groups), MTP drafters, separate Whisper processes and `ttl`. So
llama-swap stays — and with the registry in front of it you get the same convenience
plus considerably more information.
