# The llama.cpp options — explained once

You do not need this day to day; `llm add` sets everything. This page is for
looking things up, understanding what happens, and hand-tuning.

## Where the long commands live

In `config/llama-swap.yaml` under **`macros:`**. The long llama-server command is
defined there **exactly once**, and each model just references it (`${server}`).
To change an option for *all* models, change the macro — not every model.

## The options that matter

| Option | Meaning |
|--------|---------|
| `-m <file.gguf>` | which model to load |
| `-ngl 99` | how many layers go on the GPU. `99` = all of them (what we want). |
| `-c 8192` | context size (the token "memory"). Larger = more VRAM. `llm add -c 32768 …` |
| `-fa on` | flash attention — faster and leaner, leave it on. |
| `-ctk q8_0 -ctv q8_0` | quantise the KV cache: **halves** context memory, quality loss not measurable in practice. Worth it from ~64k context. |
| `-np 4 -kvu` | four **slots**: four requests are served at the same time instead of queueing. `-c` is the *total* KV either way, so slots cost no KV memory — only ~1.4 GB of extra compute buffer on a 27B. `-kvu` makes the `-c` tokens one shared pool, so a lone request may still use all of them; without it each slot gets a hard `-c / slots` share. Leaving the flag out entirely is the same as `-np 4 -kvu` (llama.cpp's auto default). See "Slots" below. |
| `--reasoning-effort low` | the **default** thinking depth for clients that send no `reasoning_effort` — which is most of them. A request that sends one still wins. Accepted values come from the model's template, not from llama.cpp; see "Thinking depth" below. |
| `--no-reasoning-preserve` | drop previous `reasoning_content` from the conversation instead of re-feeding the model its own deliberation from every earlier turn. |
| `-cram 16384` | how much **host RAM** (MiB) may hold prompt caches, so a returning agent does not re-read its whole prompt. Costs no VRAM. Default is 8192. See "Slots" below. |
| `--mmproj <file>` | vision projector for image models (see "Understanding images"). |
| `--jinja` | correct chat templates and tool calling (essential for agents and editors). |
| `--host/--port` | set by llama-swap (`${PORT}`) — do not touch. |
| `--no-webui` | turn off llama-server's own mini UI (we use Open WebUI). |
| `-ts 1,1` | tensor split: spread the model evenly over the cards. **Generated** — see below. |
| `--device ROCm0` | use only this card (what `llm add --gpu N` writes). |
| `--min-p 0` | **trap:** llama.cpp filters at `0.05` by default. Models that recommend `min_p=0` (e.g. Qwen3-Coder-Next) need this explicitly. |
| `--no-context-shift` | no automatic context shifting at the end. Required for hybrid models with recurrent state (Qwen3-Next/DeltaNet), otherwise the state goes inconsistent. |

### `-ts` is generated, not hand-written

`-ts` sits on its own line in the three chat macros and `llm gpu sync` rewrites it
to match the detected card count — nothing on one card, `1,1` on two, `1,1,1` on
three. `llm status` warns when the two no longer match.

It would be tempting to just leave `-ts` out and let llama.cpp decide. Do not:
without a tensor split, llama.cpp distributes **by free VRAM at load time**
(`llama.cpp/src/llama-model.cpp`, "default split, by free memory"). If one card
already holds something, the model lands almost entirely on the other one, and the
placement is no longer reproducible.

## Slots (several clients or agents at once)

One llama-server serves as many requests at a time as it has **slots**. With
`--parallel 1` there is exactly one, so a second client — or a subagent — waits in
a queue until the first is done. llama-swap itself allows ten concurrent requests
per model, so the queue is never its fault.

`-c` is the *total* KV cache in either case: with `-kvu` all sequences share those
tokens as one pool, without it each slot gets a hard `-c / slots` share
(`llama-context.cpp`, `n_ctx_seq`). Slots therefore cost **no** KV memory. Omitting
`-np` entirely is the same as `-np 4 -kvu`.

### What it actually buys — measured on 2× R9700, Qwen3.8-27B-Q6_K, `-c 131072`

Two clients, five steps each, ~11k tokens of session context per client:

| | `--parallel 1` | `-np 4 -kvu` |
|---|---|---|
| total wall clock | 66.6 s | 66.7 s |
| a warm step, mean | 8.1 s | **4.6 s** |
| a warm step, median | 5.8 s | 4.9 s |
| a warm step, **worst case** | **24.7 s** | **5.0 s** |
| VRAM on card 0 | 30.76 GB | 32.17 GB |

Read that carefully: the **total throughput does not change**. One card has a fixed
budget and slots do not enlarge it. What changes is *fairness*. With one slot a step
that needed 0.6 s of work randomly took 24.7 s because it sat behind another
client's prompt. With four slots every step lands between 3.4 and 5.1 s. That is the
whole point — the machine stops feeling blocked.

### Where slots do *not* help

Prompt processing (prefill) is a single, saturated resource on this hardware:

| four 15k-token prompts at once | prefill throughput |
|---|---|
| `--parallel 1` (queued) | 549 tok/s |
| `-np 4 -kvu` (interleaved) | 391 tok/s |

Four prefills interleaving in `-ub`-sized chunks is **slower** than doing them one
after another. Raising `-b 4096 -ub 1024` recovered only 2.6 % of that and cost
0.43 GB, so this stack leaves `-b`/`-ub` at llama.cpp's defaults. A single card
prefills ~560 tok/s and no flag changes that; the only real cure is a second card
with its own model (see "One, two or more GPUs").

### The flag that matters more than slots: `-cram`

Agents send almost the same prompt every step. The prompt cache means only the new
tokens are read — 1.2 s instead of 34 s in the runs above, an 80 % cache hit rate.
`-cram` caps that cache in **host RAM**, and the default is only 8192 MiB. At
roughly 36 KB per token for a 27B with a q8_0 KV cache, 8 GB holds about 228k tokens
of cached prefix — four agents with 60k contexts already exceed it, and then every
step pays the full prefill again. Same four-client test, cache artificially reduced:

| four clients, three steps | `-cram 512` | `-cram 8192` (default) |
|---|---|---|
| tokens re-read | 56 917 | 45 627 (the minimum possible) |
| wall clock | 165.0 s | 138.0 s |
| a warm step, mean | 25.6 s | 12.2 s |

The macros here set `-cram 16384`. It is a ceiling, not a reservation, and it costs
no VRAM — check `free -g` before raising it much further, since every running model
may claim its own.

### How many slots

Four is a good default and what llama.cpp picks on its own. Note the four-client
numbers above: the worst-case warm step was 18.1 s against 5.0 s with two clients.
One card comfortably serves **two or three** concurrent agents; beyond that the
prefill ceiling dominates and a second card is the answer, not more slots.

```bash
llm add --slots 4 …                                   # when adding
llm ls                                                # SLOTS column, * = -kvu
curl -s localhost:8080/upstream/<model>/props | jq .total_slots   # what is really running
```

## Token prediction (answering faster)

Speculative decoding lets the model propose several tokens at once and verifies
them in one pass → more tokens per second at identical quality. In llama.cpp this
is `--spec-type`, offered here through two ready-made macros. `llm speed` is the
short explainer with measured numbers.

### `--mtp` → macro `server-mtp` (`--spec-type draft-mtp`)

Uses the **MTP heads** of an MTP-capable model (**Qwen3.x, DeepSeek V3/R1,
Gemma 4** and similar). There are **two variants**, and `llm add --mtp` detects
both:

- **embedded** (e.g. Qwen3.6 `*-MTP-GGUF`): the MTP tensors are inside the main
  model, `--spec-type draft-mtp` is enough. Take the **right** GGUF — a standard one
  *without* MTP tensors will not start with `--mtp`.
- **separate drafter** (e.g. Gemma 4: `MTP/mtp-…-Q8_0.gguf`): `llm add --mtp` fetches
  the small Q8_0 drafter as well and appends `--model-draft <file>`. Shown as
  `[MTP+drafter]`.

**Not every Qwen3 has MTP.** Checked: `Qwen3-Coder-Next` has **no** MTP tensors in
its GGUF despite the Qwen3 base — use `--ngram` there. You can check yourself:

```bash
llama.cpp/build/bin/llama-gguf <file.gguf> r n | grep -i nextn
```

No hits means no MTP. (In a Qwen3.8 Q6_K the hits are `blk.64.nextn.eh_proj.weight`
and friends, plus the metadata key `nextn_predict_layers = 1`.)

**Tuning:** how many tokens are predicted per step is `--spec-draft-n-max` in the
model's `cmd`. Measured on Qwen3.8-27B-Q6_K:

| `--spec-draft-n-max` | code | prose |
|---|---|---|
| off (no MTP) | 21.9 tok/s | 21.9 tok/s |
| 2 | **43 tok/s** (88 % accepted) | **30.5 tok/s** (45 %) |
| 4 | 46 tok/s (80 %) | 25.8 tok/s (31 %) |

So 2 is the better all-rounder: a higher value wins slightly on code and loses
clearly on prose, because a wrong guess costs a verification pass. Qwen ≈ 2,
Gemma ≈ 4 as starting points; try 1–6 and keep the fastest (`llm edit`, then
`llm restart`).

> **Reasoning models need room for the answer.** They think in a
> `reasoning_content` block before the actual `content`. Give at least **1000**
> `max_tokens` over the API — measured, Gemma 4 spent 657 tokens before two
> sentences of answer. Otherwise `content` is empty with
> `finish_reason: length`. Open WebUI folds the thinking into a collapsible block.
>
> **Controlling thinking depth** has its own section below — the short version is
> that `reasoning_effort` is a per-request field, the accepted values are decided
> by the model's chat template rather than by llama.cpp, and `-rea off` switches
> thinking off for a whole model.
>
> Token prediction does **not** help here: it speeds up writing, not thinking, and
> thinking text is prose — the acceptance rate is roughly half what it is on code.

### `--ngram` → macro `server-ngram` (`--spec-type ngram-simple`)

Guesses the next tokens from the text so far. **No draft model, any model.**
Especially strong on code and repetitive text, and always the safe choice when
`--mtp` does not work.

### Other methods (by hand in the YAML)

llama.cpp also supports `draft-eagle3`, `draft-dflash` (a small separate draft
model) and several `ngram-*` variants. Details in `llama.cpp/docs/speculative.md`;
add a macro alongside the existing ones to use them.

## Thinking depth (`reasoning_effort`)

A reasoning model decides how long to think from a `reasoning_effort` value that
ends up in its chat template. Three things about it are easy to get wrong, and
all three cost either latency or a failed request.

### The accepted values come from the model, not from llama.cpp

`llama-server --help` lists the OpenAI set, and `/props` reports
`supports_reasoning_effort: true` — neither tells you which values the template
will accept. Qwen3.8's takes exactly three:

| sent | result |
|---|---|
| nothing | `xhigh` — the template's own default |
| `low` | "Keep your thinking brief…" |
| `medium` | **no instruction at all** — the template injects nothing |
| `xhigh` | "Please think carefully…" |
| `none` | thinking off; llama.cpp handles this before the template |
| `high`, `minimal`, `max` | **HTTP 500**, `Jinja Exception: Unexpected reasoning effort` |

So a client offering the usual low/medium/high picker fails on a third of it.
The set is in the GGUF header, so `llm ls` and the registry read it rather than
guess: `runtime.reasoningEffort.accepts` per model, and `compat.reasoningEfforts`
in what pi receives, so a client can offer only what works. Models whose template
does not gate the value report `null` — then anything goes.

### The server sets a floor, not a ceiling

Most harnesses send no `reasoning_effort` for a local model at all, which is why
"I set it to low and nothing happened" is a common complaint: the field never
leaves the client. Set the default where it cannot be skipped:

```
--reasoning-effort low
```

A request that carries the field still wins — `server-common.cpp` merges the
command-line kwargs first, then the request's `chat_template_kwargs`, then the
OpenAI field. So this lowers the default without taking control away from a
client that knows what it wants.

Measured on Qwen3.8-27B with nothing sent: `xhigh` before, `low` after.

### Old thinking accumulates unless you say otherwise

The template keeps previous `reasoning_content` blocks in the conversation by
default, so at turn five the model re-reads its own deliberation from turns one
to four and continues in the same groove. Qwen recommends against feeding
reasoning back for their own models.

```
--no-reasoning-preserve
```

A no-op if your client never returns `reasoning_content`; otherwise it drops the
old blocks. A request can force them back on with
`{"chat_template_kwargs":{"preserve_reasoning":true}}`.

`--reasoning-budget N` also exists in this build and is a real token budget
(`-1` unlimited, `0` off, `N` a cap, plus `--reasoning-budget-message` for the
text inserted before the closing tag). Treat it as an emergency brake: cutting a
thought off mid-sentence is not a setting.

### Do not switch effort mid-session

The effort instruction is rendered at the very front of the prompt, ahead of your
own system prompt. Changing it between turns therefore changes the prefix from
position 0 and throws away the whole prompt cache — on a 90k-token session that
is a full re-prefill, which costs far more than the thinking you saved. Decide
per session, not per task. The same applies to `preserve_reasoning`.

If you want both depths available at once, give them separate names rather than
toggling: two `llm add` entries pointing at the same GGUF differ only in their
flags, and `llm role` puts a stable name in front of each. That costs no extra
VRAM only if you never load both at the same time — otherwise it is two models.

## Understanding images (vision / mmproj)

Multimodal models need **two** files: the language model and the image projector
`mmproj-*.gguf` (~1 GB, in the same Hugging Face repo). **`llm add` does not fetch
it** — one extra step:

```bash
HF_HUB_DISABLE_XET=1 uvx --from huggingface_hub hf download bartowski/Qwen3.8-27B-GGUF \
  --include "mmproj-Qwen3.8-27B-f16.gguf" --local-dir models/mmproj-qwen3.8-27b
```

Then attach it to the model: `llm add … -- --mmproj <path>/mmproj-….gguf`

The projector belongs to the **model**, not the quant: one file serves every quant
of the same model, which is why it lives in its own directory and can be shared.
Images arrive over the API as `image_url` with `data:image/png;base64,…`.

## RAG services: embedding and reranking

Two more macros, neither of them chat models:

| Macro | `llm add` flag | Endpoint | Job |
|-------|----------------|----------|-----|
| `server-embed` | `--embed` | `/v1/embeddings` | text → vector. Without it a knowledge base finds nothing at all. |
| `server-rerank` | `--rerank` | `/v1/rerank` | re-sorts the hits by real relevance. |

Two details that otherwise cost an evening:

- **`-b 8192 -ub 8192`**: embedding models compute non-causally, so the batch sizes
  have to be ≥ the context. Both macros already carry it.
- **`ttl: 0`** is set automatically by `--embed`/`--rerank`, so the service models
  stay loaded. Otherwise the first search after 15 idle minutes pays for a reload
  every time. Change it with `--ttl SEC`.

**The reranker is picky about where the GGUF came from.** Qwen3-Reranker is not a
classic cross-encoder but a language model answering "yes/no". llama.cpp needs two
things to be *in the file*: the classification head `cls.output.weight` and the
template `tokenizer.chat_template.rerank`. If either is missing the server still
starts, concatenates query and document blindly, and returns **inverted** results —
in one measurement the irrelevant document scored 0.9999 and the correct passage
5e-16. So check before relying on it:

```bash
llama.cpp/build/bin/llama-gguf <file.gguf> r n | grep -E "chat_template.rerank|cls.output"
```

No hits means a bad conversion. `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` (converted
by the llama.cpp team themselves) is reliable:

```bash
llm add --rerank --gpu 1 -c 8192 ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF Q8_0
```

## Speech to text (whisper.cpp)

A separate project next to llama.cpp, built with the same HIP flags and managed
through `llm update whisper`. It runs as an ordinary llama-swap entry and is loaded
and unloaded like any model:

```yaml
  "whisper-large-v3-turbo":
    cmd: "@WHISPER_HOME@/build/bin/whisper-server -m …/ggml-large-v3-turbo-q8_0.bin
          --host 127.0.0.1 --port ${PORT} --request-path /v1/audio
          --inference-path /transcriptions -l auto"
    env: ["HIP_VISIBLE_DEVICES=1"]     # one card - but see the warning below
    checkEndpoint: "/v1/audio/health"  # moves with --request-path!
```

- **`--request-path` + `--inference-path`** turn whisper's own `/inference` into the
  OpenAI standard `/v1/audio/transcriptions`. No extra service, no shim.
- **`checkEndpoint`** has to move with it, or llama-swap looks for `/health` in the
  old place and never considers the model ready.
- Note that the card number in `env:` is an **absolute** HIP index, while
  `--device ROCmN` counts *within* the visible cards. `llm` translates between the
  two; if you edit by hand, `llm gpu list` shows the mapping.
- **Pinning through `env:` alone does not put the model in a card group.** The group
  generator reads `--device ROCmN`, so an `env:`-pinned model lands in llama-swap's
  default group, which swaps and is exclusive — starting it unloads the pinned
  models on *both* cards. `llm ls` marks this case with a `!` behind the card
  number. The whisper entries above are pinned by `env:` because whisper-server has
  no `--device` flag.
- Speed: 11 seconds of audio in 0.3 seconds, roughly 36× realtime.

## Adding a model by hand

In `llama-swap.yaml`, indented under `models:`:

```yaml
  "my-model":
    cmd: "${server} -m /path/to/llm-box/models/…/file.gguf -c 8192"
    ttl: 900       # idle seconds, then unload (frees VRAM); 0 = never unload
```

Then `llm restart`. (`llm add` is easier and also records provenance.)

## One, two or more GPUs

Which cards are visible is detected, written to `config/hardware.env` by
`llm gpu sync`, and read by the service through `EnvironmentFile`. `llm gpu list`
shows what was found, `llm doctor` checks that llama.cpp agrees.

**Default (one model across every card):** the three chat macros carry a generated
`-ts`, which spreads the model evenly. Necessary for anything larger than a single
card, e.g. a 49 GB MoE on two 32 GB cards.

**Separation (a small model pinned to one card):**

```bash
llm add --gpu 0 unsloth/Qwen3-8B-GGUF Q4_K_M
llm add --gpu 1 unsloth/Qwen3-14B-GGUF Q4_K_M
```

That appends `--device ROCmN -sm none -mg 0` to the `cmd` line (`-sm none` disables
the macro's `-ts`) **and** puts the model into the routing group in the YAML:

```yaml
groups:
  pinned:
    swap: false        # members do not evict each other
    exclusive: false   # and do not evict anything from other groups
    persistent: true   # and no other group may evict THEM
    members:
      - "qwen3.8-27b-q6_k"        # card 0
      - "qwen3-embedding-4b-q8_0" # card 1
```

Only that lets **two cards each hold a model at the same time** — without a group,
llama-swap would unload the other one on every switch. The block is generated by
`llm add`/`llm rm`/`llm gpu sync` (marker `# >>> llm:groups`); do **not** maintain
it by hand.

Three details about that block are easy to get wrong:

- **It is one group, not one per card.** The settings would be identical for every
  card anyway, and a `spillover` role (see "Roles" below) requires all of its
  targets to sit in a *single* group — which is exactly the card-0-then-card-1
  case. llama-swap refuses to start otherwise:
  `selectors.<name> spillover targets must share one routing group`.
- **`persistent: true` is what protects the pinned models.** Without it a model
  that spans all cards evicts them. Measured: loading whisper took `/running` from
  `[embedding, qwen3.8-27b]` down to `[whisper]` alone.
- **Both ways of pinning count.** whisper-server has no `--device` flag and pins
  its card through `env: HIP_VISIBLE_DEVICES=N`; the group generator reads that
  too. `llm ls` puts a `!` behind the card number of anything pinned but ungrouped.

**Changing placement later** — without `llm edit`, also from another machine or by
an agent (details in [API.md](API.md)):

```bash
curl -X PATCH http://<server-ip>:8081/api/models/<name> \
     -H "X-LLM-Token: $(llm api token)" -H 'Content-Type: application/json' \
     -d '{"gpu":"both"}'          # "both" | a card number  (?dryRun=true shows the diff)
```

That sets or removes exactly the flags described here, regenerates the `groups`
block and restarts llama-swap. It first works out whether weights plus KV cache fit
on the target — **per card, not as a sum**: with an even `-ts` a 30 GB model needs
15 GB on *each* card, so a lot of free space on one of them does not help. You get
`409` instead of a llama-server that dies at load time.

Things worth knowing:

- **MTP models with a separate drafter** (e.g. Gemma 4) additionally need
  `--spec-draft-device ROCmN` when pinned — otherwise llama-server dies with
  `ggml_abort` while measuring the draft model. `llm add --mtp --gpu N …` sets it.
- `swap: false` also means several models on the **same** card all stay loaded,
  which can fill the card. The safety net is `ttl` (default 900 idle seconds).
- A **large** model (no `--gpu`) needs every card, but since the group is
  `persistent` it no longer unloads the pinned ones. Both then compete for the
  same VRAM, so check `llm gpu` if a large model refuses to load.
- ComfyUI gets one card, chosen by `llm gpu sync` and overridable with
  `LLM_COMFY_GPU`. For real quiet, pin the LLM to a different card than ComfyUI.

## Roles (one name, several models — this is how subagents get their own)

A `selectors:` block gives llama-swap a **virtual model name** that it resolves to
a real model per request. Clients and their subagents then never need to know a
file name, and the placement stays a server-side decision:

```bash
llm role                                   # what exists and what it resolves to
llm role set chat warm  <big> <medium>     # whichever is already loaded
llm role set fast  pin   <small>           # always this one
llm role set coder spillover <big> <small> --spillover=2
llm role rm  coder
```

| strategy | behaviour |
|---|---|
| `warm` | the first target that is already running; cold-starts the first one if none are. Saves a 30 GB load for a one-line question. |
| `pin` | always the first target. The remaining entries are documentation. |
| `spillover` | the first target up to `--spillover=N` concurrent requests, then the **next** target starts. With one model per card that is automatic card-0-then-card-1. |

`spillover` is the one that answers "how do my subagents get the second card":

```
4 concurrent requests to role "coder", spillover=2, on 2× R9700
  request 0 -> Qwen3.8-27B-Q6_K   (card 0)   12.3 s
  request 1 -> Qwen3.5-4B-Q4_K_M  (card 1)    4.9 s
  request 2 -> Qwen3.5-4B-Q4_K_M  (card 1)    5.2 s
  request 3 -> Qwen3.8-27B-Q6_K   (card 0)   12.1 s
```

Both models were loaded at the same time, one per card, and the client only ever
asked for `coder`.

**A role never promises more than its weakest target.** Its context window is the
**minimum** over the targets and its capabilities are the **intersection** — pair a
131k vision model with an 8k text-only one and the role reports 8k and no vision,
because a client that trusted the larger number would fail on every second request.
`llm role` prints the effective value, so check it after changing targets.

Roles appear in `GET /v1/models`, so Open WebUI lists them without any setup, and
in the registry catalog with `"kind": "role"`, so pi offers them as models. They are
written into the marker block `# >>> llm:selectors` — do not edit it by hand.
Constraints worth knowing: a role name must not collide with a model name, a role
cannot target another role, and `spillover` targets must share one routing group
(see above).

## Build flags

`llm update llama` and `llm update whisper` share one set of HIP flags. The two that
depend on your hardware are detected, not hardcoded:

- `-DAMDGPU_TARGETS=…` from `rocm-smi --showproductname` (only the discrete cards —
  the iGPU reports a different gfx target and would be wasted build time)
- `-DCMAKE_HIP_COMPILER=…` from `hipconfig --hipclangpath`

Override with `LLM_GFX_TARGETS` / `LLM_HIP_COMPILER` if the detection is wrong.
`-DGGML_NATIVE=ON` means the build is tuned for the CPU it was built on, and
`-DGGML_SCHED_MAX_COPIES=4` is multi-GPU pipeline tuning.

## Useful checks

```bash
llm gpu          # rocm-smi: VRAM, temperature, load
llm gpu list     # cards as this stack sees them, and what is pinned where
llm logs         # what llama-swap and the model are doing
llm ps           # which model is loaded right now
llm doctor       # everything at once
```
