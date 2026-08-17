# Models — choosing, adding, tuning

You do not need to remember llama.cpp command lines. Adding a model is one line.

## Three steps to a new model

1. **Find a GGUF model** on Hugging Face: <https://huggingface.co/models?library=gguf>.
   Publishers with good ready-made GGUFs: **unsloth**, **bartowski**, **ggml-org**.
2. **Copy the repo id** (e.g. `unsloth/Qwen3-8B-GGUF`).
3. **Add it:**
   ```bash
   llm add unsloth/Qwen3-8B-GGUF Q4_K_M
   ```
   That downloads it, works out its settings, checks that it fits, writes the
   configuration, and makes it available through the API and in the browser.

`llm search <name>` prints this guidance any time, with the VRAM figures of the
machine you are on.

> **If a download crawls:** Hugging Face uses the "Xet" transfer backend, whose
> client sometimes throttles itself to ~50 KB/s while reporting "connection
> struggling" — a 23 GB quant would take over 15 hours. `llm add` therefore sets
> `HF_HUB_DISABLE_XET=1`, which gets 7–15 MB/s. Interrupted downloads resume where
> they stopped, so just run the command again.

## Which quant?

The suffix trades quality against size:

| Quant | Quality | Size (8B example) | When |
|-------|---------|-------------------|------|
| `Q4_K_M` | very good | ~5 GB | **the default**, almost always fits |
| `Q5_K_M` | better | ~6 GB | when you have VRAM to spare |
| `Q6_K` / `Q8_0` | near lossless | ~7–9 GB | small models, maximum quality |

Rough sizes at `Q4_K_M`: 8B ≈ 5 GB · 14B ≈ 9 GB · 32B ≈ 20 GB · 80B MoE ≈ 49 GB.

Two things to keep in mind:

- **A model needs roughly the size of its file, plus context.** The KV cache is on
  top of the weights — around 1.5 GB per 16k tokens on a 32B model, depending on
  the architecture. `llm add` reads the real geometry out of the GGUF header and
  refuses a model that will not fit, per card (`--force` overrides).
- **A model that fits on one card can be pinned there**, which lets a second model
  stay loaded at the same time. Above that it is spread across all cards.

On large dense models the quant decides whether one card is enough. Measured on a
32 GB card with Qwen3.8-27B: `Q6_K` = 23.5 GB and fits on one card with a 131k
context (30.7 of 34 GB used), while `Q8_0` = 29.1 GB needs two cards but then
allows 262k. At that much context add `-ctk q8_0 -ctv q8_0 --parallel 1`, otherwise
the KV cache eats everything — see [FLAGS.md](FLAGS.md).

**Vision models** additionally need their `mmproj-*.gguf`, which `llm add` does
*not* fetch automatically — see FLAGS.md, "Understanding images".

## Going faster: token prediction

Two options when adding (details in [FLAGS.md](FLAGS.md), explainer in `llm speed`):

```bash
llm add --mtp   unsloth/Qwen3-30B-A3B-GGUF Q4_K_M                  # MTP heads in the model
llm add --ngram bartowski/Qwen2.5-Coder-14B-Instruct-GGUF Q5_K_M   # universal, great for code
```

- `--mtp` only works on MTP-capable models (Qwen3.x, DeepSeek V3/R1, Gemma 4) whose
  GGUF actually contains the MTP tensors. If it does not, the server refuses to
  start — load it without `--mtp`.
- `--ngram` works with **any** model and needs no draft model. Strongest on code.

Measured on Qwen3.8-27B-Q6_K with MTP: 21.9 → 43 tok/s on code (88 % of guesses
accepted) and 21.9 → 30.5 tok/s on prose (45 %).

## Pinning small models to separate cards

```bash
llm add --gpu 0 unsloth/Qwen3-8B-GGUF Q4_K_M      # card 0 only
llm add --gpu 1 unsloth/Qwen3-14B-GGUF Q4_K_M     # card 1 only
```

Both stay loaded **at the same time** and do not evict each other — useful for
querying two models in parallel, or for keeping a card free for ComfyUI.
`llm gpu list` shows which model is pinned where. The mechanism (`--device` plus
GPU groups in the YAML) is described in FLAGS.md, "One, two or more GPUs".

Without `--gpu` a model uses every card, and doing so unloads the pinned ones.

## Settings per model

There are two levels, and the difference matters.

**1. Fixed when the model loads** (stored in the configuration):

```bash
llm add -c 32768 -t 0.7 unsloth/Qwen3-14B-GGUF Q4_K_M
```

- `-c` context size (the token "memory", default 8192). More context, more VRAM.
- `-t` default temperature (0 = strict and factual, 0.7–0.8 = more creative).
- Any further llama-server flags after `--`:
  ```bash
  llm add unsloth/Qwen3-14B-GGUF Q4_K_M -- --top-p 0.9 --min-p 0.05 --repeat-penalty 1.1
  ```
  The full list: `llama.cpp/build/bin/llama-server --help`.

**2. Per request**, overriding those defaults without reloading anything:

```bash
curl … -d '{"model":"qwen3-14b-q4_k_m","temperature":0.3,"top_p":0.9,
            "max_tokens":500,"reasoning_effort":"low","messages":[...]}'
```

In Open WebUI the same knobs sit behind the chat settings. **What the client sends
wins** — `-t` is only the fallback when the request says nothing.

To change settings later, without downloading again:

```bash
llm edit        # open the configuration, adjust the model's cmd line, save
llm restart
```

or simply add it again — the file is cached, so nothing is re-downloaded:

```bash
llm rm qwen3-14b-q4_k_m     # answer "n" to keep the files
llm add -c 16384 -t 0.5 unsloth/Qwen3-14B-GGUF Q4_K_M
```

`ttl` (idle seconds before unloading) can also be set directly in the
configuration — see FLAGS.md.

> **Reasoning models need room for the answer.** Models like Qwen3.8 and Gemma 4
> write a thinking block first and the answer after it. With `max_tokens` too small
> you get an **empty** `content` and `finish_reason: length` — nothing is broken,
> the model was still thinking. Measured: Gemma 4 spent 657 tokens before two
> sentences of answer. So allow 1000+, or shorten the thinking with
> `reasoning_effort` (see FLAGS.md).

## More than chat: embeddings, reranking, speech

Everything hangs off the **same** endpoint. Only the `model` name decides what
happens:

| Role | What it does | Endpoint |
|------|--------------|----------|
| chat | conversation, tools, vision | `/v1/chat/completions` |
| `--embed` | make documents searchable (RAG) | `/v1/embeddings` |
| `--rerank` | sort RAG hits by real relevance | `/v1/rerank` |
| whisper | dictation → text | `/v1/audio/transcriptions` |

```bash
llm add --embed  Qwen/Qwen3-Embedding-4B-GGUF Q8_0
llm add --rerank Qwen/Qwen3-Reranker-0.6B-GGUF Q8_0
```

Both are configured with `ttl: 0`, i.e. they stay loaded — otherwise every search
after 15 idle minutes would pay for a reload.

### A worked example: a "service card"

On a two-card machine it is worth keeping one card for small always-on models and
letting the other hold the big chat model. Pinning an embedder, a reranker and a
small task model to card 1 was measured at **21.7 GB of 34 GB** — much more than
the 7.7 GB their files add up to, because at `-b/-ub 8192` the batch buffers and KV
caches come on top. That leaves room for Whisper (~1.6 GB) but not for another
large model. If you need space, turn the context of those service models down from
8192 with `llm edit`; it feeds straight into the buffer sizes.

A model **without** `--gpu` needs every card and briefly evicts the pinned ones.
They come back by themselves on the next request.

One small model earns its place especially: Open WebUI generates chat titles and
tags with whichever model is currently active. Without a dedicated task model, a
27B reasoning model thinks at full depth about a chat headline. A 4B model with
`-rea off` does the same job in milliseconds.

## Managing

```bash
llm ls                      # all configured models
llm meta                    # which Hugging Face repo each one came from
llm rm qwen3-8b-q4_k_m      # remove (asks whether to delete the files too)
```

## Using them

- **Browser:** the chat UI on port 3000, model picked from the dropdown.
- **API / code:** `http://<server-ip>:8080/v1`, with a `model` name from `llm ls`.
  ```bash
  curl http://<server-ip>:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3-8b-q4_k_m","messages":[{"role":"user","content":"Hello"}]}'
  ```
  Any non-empty API key is accepted — llama.cpp does not check the value.

`llm url` prints the addresses for the machine you are on.

## Agents on other machines

Nothing needs to be maintained on the client side. The registry on port 8081
answers which models exist, how they are configured and where they came from — and
accepts changes:

```bash
llm api url          # addresses (catalog, MCP, OpenAPI docs)
llm api token        # the key an agent needs in order to change anything
llm api client       # ready-made setup line to paste on a client machine
```

See [API.md](API.md) for the endpoints and MCP, and [PI.md](PI.md) for the pi
integration.

## Reusing GGUFs you already have

Models downloaded by another tool do not have to be fetched again. Move or symlink
the `.gguf` into `models/<name>/` and add an entry by hand with `llm edit`, using an
existing entry as the template. `llm meta backfill` then tries to work out which
Hugging Face repo it came from.
