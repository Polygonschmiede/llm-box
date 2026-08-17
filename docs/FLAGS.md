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
| `--parallel 1` | only **one** slot. Otherwise llama.cpp divides the `-c` tokens among slots — at `-c 131072` with 4 slots a single request only gets 32k. |
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
> **Controlling thinking depth.** Templates like Qwen3.8's default to `xhigh` and
> think accordingly long. That is a per-request decision and needs no reload —
> llama-server accepts the plain OpenAI field:
> ```bash
> curl … -d '{"model":"qwen3.8-27b-q6_k","reasoning_effort":"low","messages":[...]}'
> ```
> Values: `none` · `low` · `medium` · `high` · `xhigh`. Measured on a trivial
> question: `low` spent ~25 thinking tokens, `xhigh` ~30, and `none` switched
> thinking off entirely (4 tokens total instead of 62). Rule of thumb: extraction
> and RAG answers `low`–`medium`, real puzzles `high`+. The server flag `-rea off`
> disables thinking for a whole model.
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
    env: ["HIP_VISIBLE_DEVICES=1"]     # one card, so the other keeps the big model
    checkEndpoint: "/v1/audio/health"  # moves with --request-path!
```

- **`--request-path` + `--inference-path`** turn whisper's own `/inference` into the
  OpenAI standard `/v1/audio/transcriptions`. No extra service, no shim.
- **`checkEndpoint`** has to move with it, or llama-swap looks for `/health` in the
  old place and never considers the model ready.
- Note that the card number in `env:` is an **absolute** HIP index, while
  `--device ROCmN` counts *within* the visible cards. `llm` translates between the
  two; if you edit by hand, `llm gpu list` shows the mapping.
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
the macro's `-ts`) **and** puts the model into a GPU group in the YAML:

```yaml
groups:
  gpu0:
    swap: false        # models in the same group do not evict each other
    exclusive: false   # and do not evict anything from other groups
    members: ["…"]
```

Only that lets **two cards each hold a model at the same time** — without groups,
llama-swap would unload the other one on every switch. The block is generated by
`llm add`/`llm rm`/`llm gpu sync` (marker `# >>> llm:groups`); do **not** maintain
it by hand.

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
- A **large** model (no `--gpu`) needs every card and unloads the pinned ones while
  it runs. That is intended; they come back on the next request.
- ComfyUI gets one card, chosen by `llm gpu sync` and overridable with
  `LLM_COMFY_GPU`. For real quiet, pin the LLM to a different card than ComfyUI.

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
