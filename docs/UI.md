# The interfaces, and which one knows what

There are four, they overlap, and each is authoritative for something different.
Using the wrong one for a question is how you end up with a number that
contradicts another number.

| Where | Port | Good for | Blind to |
|---|---|---|---|
| **llama-swap's own UI** | `:8080/ui` | live state, per-request telemetry, logs, GPU charts, a playground | every setting, and the card list is wrong here (see below) |
| **Open WebUI** | `:3000` | chatting, RAG, documents | hardware, contexts, load state, provenance |
| **The registry** | `:8081/docs` | every configured detail, as JSON | it is Swagger, not a dashboard |
| **`llm` on the server** | — | the whole picture, correctly | one-shot text, no live refresh |

## llama-swap's UI — the one nobody mentions

It ships with llama-swap and is not this project's work, which is probably why
nothing here pointed at it. Open `http://<server-ip>:8080/ui`. Ten pages:

- **Models** — what exists, what is loaded, capability tags, an unload button.
  Roles appear here under *Selectors* with their strategy and targets.
- **Activity** — one row per request with input/output tokens, prompt tok/s,
  generation tok/s, duration, and `draft_tokens`/`draft_acc_tokens`. That last
  pair is the acceptance rate of the token prediction from
  [FLAGS.md](FLAGS.md), measured on your actual traffic rather than a benchmark.
- **Logs** — proxy and upstream, filterable, live.
- **Performance** — GPU utilisation, VRAM, power, CPU, network, load average.
- **Playground** — chat, images, speech, transcription, rerank, and a
  concurrency load test. Handy for reproducing something without a client.
- **Hardware**, **Settings** — see the warning.

### Two things to know before you trust it

**Its Hardware page does not know about `HIP_VISIBLE_DEVICES`.** It reports every
accelerator the driver exposes, so on a machine with an integrated GPU it lists
one card too many, and its Performance page charts the iGPU as if it were a
compute card. `llm gpu list` applies the filtering and is the right answer.

**It has no configuration view at all** — `/api/config` does not exist. It cannot
show you `-cram`, the slot count, `-kvu`, the card groups, `ttl`, provenance or a
VRAM budget, because it never reads the YAML. For any question of the form "how
is this set up", use `llm ls`, `llm role` and the registry.

**`GET /unload` unloads everything** and needs no authentication. It is a
mutating GET, so a browser prefetch or anything that follows links can empty
your VRAM. See [../SECURITY.md](../SECURITY.md); the short answer is not to have
port 8080 reachable from anywhere you do not control.

## What only the CLI knows

`llm status` is the only place that assembles all of it at once, and the only
one that cross-checks: it warns when the configured `-ts` no longer matches the
cards. Beyond that:

```bash
llm ls          # contexts, slots, card, ttl per model - and a ! for a model
                # pinned to a card but in no group, so anything else evicts it
llm role        # roles with their EFFECTIVE context, which is the smallest of
                # their targets - the trap a role makes easy to walk into
llm gpu list    # the filtered card list, with what is pinned where
llm versions    # installed builds and what you can roll back to
llm doctor      # everything that can be misconfigured, with the fixing command
```

None of that is reachable over HTTP today, which is why the answer to "is there
a UI for the settings" is currently "the CLI, or Swagger".

## The registry's Swagger page

`http://<server-ip>:8081/docs`, from FastAPI. Every endpoint from
[API.md](API.md), executable from the browser: paste the contents of
`config/api-token` into the `x-llm-token` header box and the write operations
work, including `PATCH ?dryRun=true`, which shows you the before/after command
line without changing anything. It is a developer surface — rich, and no help at
all for "which card is hot".

Reads on 8081 need no token and expose every filesystem path, checksum and
Hugging Face repo on the machine. Same caveat as port 8080.
