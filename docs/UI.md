# The interfaces, and which one knows what

There are four, they overlap, and each is authoritative for something different.
Using the wrong one for a question is how you end up with a number that
contradicts another number.

| Where | Port | Good for | Blind to |
|---|---|---|---|
| **The settings page** | `:8081/ui` | what is configured and changing it: models, roles, cards, versions — including updating and rolling back | anything about a request in flight |
| **llama-swap's own UI** | `:8080/ui` | live state, per-request telemetry, logs, GPU charts, a playground | every setting, and the card list is wrong here (see below) |
| **Open WebUI** | `:3000` | chatting, RAG, documents | hardware, contexts, load state, provenance |
| **The registry** | `:8081/docs` | every configured detail, as JSON | it is Swagger, not a dashboard |
| **`llm` on the server** | — | the whole picture, correctly | one-shot text, no live refresh |

## The settings page

`http://<server-ip>:8081/ui`. One file plus a verbatim copy of the design system
in `web/vendor/stellar` — still no build step, still nothing fetched from
anywhere but this server, same origin as the API. It follows your system's
light/dark setting on its own; there is no switch to find. Four views:

- **Models** — the table from `llm ls`, and per model: **VRAM estimated against
  what is free**, per card rather than as a sum (with an even tensor split a
  30 GB model needs 15 GB on *each* card, and room on one of them does not
  help); provenance with a link to the Hugging Face repo; the configured context
  against what the model was trained for; slots with a sentence on what `-kvu`
  means; which `reasoning_effort` values the template accepts and what this
  server sends when the client sends nothing; and the full command line.
  Changing a card, context, slot count or ttl goes through `?dryRun=true` first,
  so you approve a before/after diff — or read why it would not fit.
- **Roles** — create, edit and delete, with the **effective context** in front:
  a role reports the smallest of its targets, so adding one small model shrinks
  the whole role. That trap is the reason this view exists.
- **Cards** — the filtered card list with junction temperature, power draw and
  utilisation per card, what is pinned where, the `llm gpu sync` diff when the
  configuration has drifted, and the groups with their
  `swap`/`exclusive`/`persistent` flags spelled out. The same three figures are
  in `llm status`, `llm gpu list` and `llm watch`, from the one `rocm-smi` query
  in `lib/llmreg.py` — a sensor the driver does not answer reads `?` rather than
  a confident zero.
- **System** — versions with what is newer and what you can roll back to, and
  **the buttons to do it**: *check now* asks upstream for the newest releases,
  and per engine *update* or *back* starts the same work `llm update` and
  `llm rollback` do on the server. Each is a job whose log you can follow while
  it runs; only one runs at a time. What is newer compares **commits**, not tag
  names — whisper.cpp publishes one commit under two of them, which used to read
  as a permanent update. See [UPDATES.md](UPDATES.md).

### Everything short explains itself

`Q4_K_M`, `-kvu`, `q8_0`, `gfx1201`, `ttl`, `spillover`, `-ts` — each of those is
the right word and none of them explains itself, and writing the explanation
next to every occurrence would drown the numbers the page exists to show. So
they are marked with a dotted underline and **say what they mean on hover**,
including every flag inside a command line, a macro body and a dry-run diff —
which is the only place those flags are visible at all.

The wording is this repository's own: `FLAGS.md` for the flags and the routing
strategies, `MODELS.md` for the quants, `API.md` for the recorded provenance,
cut to a sentence each. Quant names are generated rather than listed, so
`UD-Q5_K_XL` gets a sentence too.

Two limits worth knowing. The underlined terms are reachable with the keyboard
(Tab to one, Escape to dismiss), but **the flags inside a `<pre>` are hover-only
on purpose** — thirty extra tab stops in one command line would cost more than
the missing focus. And a term the page does not know renders as plain text, so a
missing entry looks like nothing rather than like an empty tooltip.

Reading needs nothing. Writing needs the token once: **sign in** exchanges the
contents of `config/api-token` for an `HttpOnly` session cookie, so the token
never sits in a form field. `llm api token` prints it on the server.

Deliberately absent: charts, logs, a playground, per-request telemetry. That is
llama-swap's UI below, and it does it better.

### Where the look comes from

[Stellar DS](https://github.com/Polygonschmiede/stellar-ds) —
`@polygonschmied/stellar-tokens`, vendored as two CSS files under
`web/vendor/stellar` and served from `/ui/stellar.css`. The page's own `<style>`
block is unlayered and therefore beats the whole `@layer stellar` stack, so it
holds only what the design system does not cover or covers differently than a
page of dense tables wants — the tables themselves, the sticky header, the
`hidden` guard, the glossary. `web/vendor/stellar/README.md` has the version and
the command that updates it; `tests/ui-matrix.sh` checks that every class and
token the page asks for still exists afterwards, which is the failure an upgrade
actually produces.

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
your VRAM. `llm key new` closes it — with a key in force, everything here except
`/health` answers 401 without `Authorization: Bearer <key>`, this page included.
See [../SECURITY.md](../SECURITY.md).

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

`llm doctor` in particular is still server-side only: what it checks — tools on
`PATH`, group membership, the venvs, the systemd units — is not meaningful over
HTTP, so the settings page links to the command rather than pretending to
replace it.

## The registry's Swagger page

`http://<server-ip>:8081/docs`, from FastAPI. Every endpoint from
[API.md](API.md), executable from the browser: paste the contents of
`config/api-token` into the `x-llm-token` header box and the write operations
work, including `PATCH ?dryRun=true`, which shows you the before/after command
line without changing anything. It is a developer surface — rich, and no help at
all for "which card is hot".

Reads on 8081 need no token and expose every filesystem path, checksum and
Hugging Face repo on the machine. Same caveat as port 8080.
