# The registry API — the server tells agents what exists

Port **8081**, next to the actual LLM API on 8080. Any agent — pi, Claude Code, a
script — asks here **which models exist right now, how they are configured, where
they came from** and what is free on the cards. And it can change them without
logging in.

Nothing is exported or cached: every response is built on the spot from
`config/llama-swap.yaml`, the provenance files and llama-swap's live state. A deleted
model is gone immediately; a moved model is moved immediately.

```bash
llm api status | on | off | restart | logs
llm api url        # the addresses
llm api token      # the key for making changes
llm api client     # ready-made setup line to paste on a client machine
```

## Reading

| Endpoint | Content |
|---|---|
| `GET /api/health` | is everything up? versions, model count, what is loaded, **`problems`** (missing files, unknown provenance) |
| `GET /api/models` | **the catalog** — everything about every model (fields below) |
| `GET /api/models?slim=true` | short form, good for agents and `jq` |
| `GET /api/models?role=chat` | filter by role (`chat`, `embed`, `rerank`, `stt`) |
| `GET /api/models/{id}` | one model |
| `GET /api/gpus` | per card: VRAM total/used/free, temperature, name, pinned models |
| `GET /api/state` | what is loaded, on which port, plus the cards |
| `GET /api/pi-models.json` | the same catalog as a ready `models.json` for pi (fallback) |
| `GET /api/events` | SSE stream announcing configuration and load-state changes (the pi extension listens on this) |
| `GET /docs` | interactive OpenAPI interface |

```bash
curl -s 'http://<server-ip>:8081/api/models?slim=true' | jq -r \
  '.[] | "\(.id)  \(.gpu)  ctx=\(.contextWindow)  \(.source.repo)"'
```

## What the catalog contains

```jsonc
{
  "id": "qwen3.8-27b-q6_k",
  "kind": "model",                      // model | role  (see "Roles" below)
  "role": "chat",                       // chat | embed | rerank | stt
  "state": "ready",                     // ready | unloaded | unknown
  "ttl": 900,                           // idle seconds before unloading (0 = never)
  "runtime": {
    "macro": "server-mtp",
    "contextWindow": 131072,
    "gpu": { "mode": "single", "device": 0, "group": "pinned", "via": "flag" },
    "specDecoding": "mtp",              // mtp | ngram | none
    "kvCacheQuant": "q8_0",
    "parallel": 4, "kvUnified": true,   // slots; no -np flag means 4 + shared KV
    "mmproj": "…/mmproj-Qwen3.8-27B-f16.gguf",
    "cmd": "…"                          // the complete llama-server line
  },
  "capabilities": { "chat": true, "tools": true, "vision": true, "reasoning": true, … },
  "sampling": { "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0 },
  "files": { "model": { "path": "…", "sizeBytes": 23463130720, "sha256": "…" } },
  "vram": { "weightsBytes": …, "kvCacheBytes": …, "estimatedBytes": … },
  "source": { "repo": "bartowski/Qwen3.8-27B-GGUF", "quant": "Q6_K",
              "revision": "f0eec4a4bb49…", "verified": true,
              "url": "https://huggingface.co/bartowski/Qwen3.8-27B-GGUF",
              "addedAt": "2026-08-16T15:02:11Z" },
  "architecture": { "arch": "qwen35", "layers": 65, "nativeContext": 262144 },
  "endpoints": { "base": "http://<server-ip>:8080/v1", "path": "/chat/completions" },
  "pi": { … }                           // ready-made models.json entry for pi
}
```

Everything except `source` is **derived** from the `cmd` line and the GGUF header —
there is no second list that could go stale. `source` comes from
`models/<name>/.llm-model.json` (see below).

The `slim=true` form additionally carries `gpuMode` and `gpuDevice` as separate
fields, so an agent does not have to parse the human-readable `gpu` string.

**Slots:** `parallel` is what llama-server will really do, not the literal flag —
llama.cpp treats a missing `-np` as four slots with a unified KV cache
(`server.cpp`), so the catalog reports `4`, not `1`. `kvUnified` says whether the
`contextWindow` is one shared pool (a single request may use all of it) or a hard
per-slot share. Both are `null` for `role: "stt"`, because whisper-server has
neither. Writable through `PATCH` as `parallel`.

**The VRAM figure is calculated, not guessed:** from the GGUF header (layers, KV
heads, key/value length) plus the KV quantisation, including the awkward cases —
hybrid models with `full_attention_interval` (Qwen3.x: only every 4th layer has a KV
cache) and sliding-window layers (Gemma 4). Cross-check: for Qwen3.8-27B-Q6_K at
131k context the estimate says 29.3 GB and the measurement is 30.7 GB.

### Roles

Entries with `"kind": "role"` are **not** models: they are virtual names that
llama-swap resolves per request (`llm role` on the server, `selectors:` in the
YAML). They carry no `files`, no `source`, no `vram` and `runtime.cmd` is `null`:

```jsonc
{
  "id": "coder",
  "kind": "role",
  "role": "chat",                       // the role of its targets
  "state": "ready",                     // ready as soon as ONE target is loaded
  "activeTargets": ["qwen3.8-27b-q6_k"],
  "runtime": {
    "selector": { "strategy": "spillover",   // warm | pin | spillover
                  "targets": ["qwen3.8-27b-q6_k", "qwen3.5-4b-q4_k_m"],
                  "spillover": 2 },
    "contextWindow": 8192,              // the MINIMUM over the targets
    "gpu": { "mode": "role", "device": null, "group": "pinned", "via": "selector" },
    "cmd": null
  },
  "capabilities": { … },                // the INTERSECTION over the targets
  "pi": { … }
}
```

`contextWindow` and `capabilities` are deliberately the weakest common denominator:
a client that trusted the larger of two targets would fail on every second request.
Filtering by `?role=chat` returns roles too, which is what makes them appear in pi.
They are **read-only** over this API — `PATCH`, `load` and `DELETE` apply to models;
change a role with `llm role` on the server.

### About card numbers

Two numbering spaces exist and the API always reports the **logical** one — the
position among the discrete cards, which is also the `N` in `--device ROCmN`.
`HIP_VISIBLE_DEVICES` uses **absolute** indices as `rocm-smi` counts them, and those
can differ when a machine has an iGPU. `GET /api/gpus` returns both (`index` and
`smiIndex`), and the translation happens inside the server so callers never have to
do it.

Per card it also reports `tempJunctionC`, `powerW` and `busyPercent` — one
`rocm-smi` query, cached for a second so several callers within one request do
not each pay for it. A field the driver does not answer is `null`, never `0`.
`busyPercent` is the share of time the GPU had work in flight and says nothing
about how much of the chip was busy, so a memory-bound decode can read 100 %
while most of the card idles.

## The endpoints the settings page needs

Four reads that nothing exposed before, which is why the configuration had no
interface but the CLI:

| Endpoint | Answers |
|---|---|
| `GET /api/versions` | active version, what is newer, and what you can roll back to, per engine. "Newer" comes from the cache `llm update` refreshes — never a live GitHub call, so this never blocks. `upToDate` compares **commits**, not tag names: whisper.cpp ships one commit as `bNNNN` and as `v1.x.y`. |
| `GET /api/config` | the parts of the YAML that are not per-model: the macros, and the groups with their `swap`/`exclusive`/`persistent` flags. llama-swap has no `/api/config` at all, so the eviction semantics were visible only by opening the file. |
| `GET /api/config/diff` | what `llm gpu sync` would change. An empty diff means the configuration matches the cards. |
| `GET /api/roles` | the roles as configured. |

And roles became writable, which they were not — `llm role` on the server was
the only way in, so a UI or a remote agent could see a role and not change it:

| Endpoint | Effect |
|---|---|
| `PUT /api/roles/{name}` | create or replace. `?dryRun=true` validates and returns the result without writing. |
| `DELETE /api/roles/{name}` | remove it. |

```bash
curl -X PUT http://<server-ip>:8081/api/roles/coder \
     -H "X-LLM-Token: $T" -H 'Content-Type: application/json' \
     -d '{"strategy":"spillover","targets":["big-model","small-model"],"spillover":2}'
```

The same rules the CLI enforces apply: the strategy must be `warm`, `pin` or
`spillover`, every target must be a configured model, a role's name must not
collide with a model's, and `spillover` targets have to share one routing group.
Removing a *model* also prunes it from every role that pointed at it and deletes
a role left with no targets — llama-swap validates selector targets at startup,
so leaving one behind would take the endpoint down on the next restart.

### Updating over HTTP

`component` is one of `llama`, `swap`, `whisper`, `ui`, `comfy` — plus `all` for
an update. It is checked against that list, not against a pattern: the value
becomes part of a command line. So does `version`, which has to look like a tag.

These return **202** with a `jobId`; the work runs in the background and
`GET /api/jobs/{id}` carries the log. Only **one** update or rollback runs at a
time — a second one gets **409**, because they share the repositories, the build
symlinks and the services. The job log lives in the registry process, so a
restart of it loses the log but not the update; the full build output is written
to a file next to the repository either way.

What actually happens is [UPDATES.md](UPDATES.md): fetch, build, smoke test, and
only then switch. A build can run for tens of minutes and llama-swap is away for
a few seconds when the symlink moves. If the smoke test fails, the version that
is running keeps running.

```bash
curl -X POST http://<server-ip>:8081/api/updates/whisper -H "X-LLM-Token: $T"
# {"jobId":"9f1c...","argv":["update","whisper"],"hint":"progress: GET /api/jobs/9f1c..."}
curl -s http://<server-ip>:8081/api/jobs/9f1c... | jq -r '.log[]'
```

### Serving the page

`GET /ui` is the control page and `GET /ui/{asset}` its stylesheets — currently
`stellar.css` and `stellar-auto-dark.css`, the vendored design system under
`web/vendor/stellar`. The name is looked up in a fixed table rather than joined
onto a directory: this process reads every model file and `config/api-token`, so
a path parameter that reaches the filesystem is how that becomes someone else's
shell. An unknown name is a 404, and there is no traversal to attempt.

## Sessions (so a page need not hold a secret)

`POST /api/session` with `{"token": "..."}` returns an `HttpOnly`,
`SameSite=Strict` cookie that counts as the token for writes. `GET /api/session`
reports `canWrite` — the page asks before it draws a single button — and
`DELETE /api/session` ends it. Sessions live in memory, so a restart of the
registry ends them all.

The cookie is **not** marked `Secure`: this project serves plain HTTP, and the
flag would make the cookie unusable rather than the connection safe. Over an
untrusted network use the SSH tunnel from [REMOTE.md](REMOTE.md).

**Reads stay open by default.** That is deliberate rather than an oversight: the
pi extension fetches `/api/models`, `/api/gpus`, `/api/health` and `/api/jobs`
without a token, and [PI.md](PI.md) tells people the token is only for changes.
Set `LLM_API_REQUIRE_AUTH=1` in the service environment to require it for reads
too — worth doing if port 8081 is reachable beyond your own machine, since reads
return every model path, checksum and Hugging Face repo along with live VRAM.
`/api/health` and `/api/pi-models.json` stay open either way, or nothing could
check reachability.

That variable covers MCP too. It did not: `list_models`, `get_model`,
`gpu_status` and `job_status` answered without a token whatever it said, which
made `LLM_API_REQUIRE_AUTH=1` a half-closed door — the same catalog and the same
filesystem paths, reachable through `/mcp` instead of `/api`.

## Changing (header `X-LLM-Token`)

The key lives in `config/api-token`, generated on first start, readable only by you:
`llm api token`.

| Endpoint | Effect |
|---|---|
| `PATCH /api/models/{id}` | `gpu`, `contextWindow`, `parallel`, `ttl`, `sampling`, `piOverrides`, `force` |
| `PATCH /api/models/{id}?dryRun=true` | check and show before/after — change nothing |
| `POST /api/models/{id}/load` | pull a model into VRAM |
| `POST /api/unload` | unload everything (llama-swap only knows all-or-nothing) |
| `POST /api/models` | fetch a new model from Hugging Face → returns a job |
| `GET /api/jobs`, `GET /api/jobs/{id}` | progress and log of downloads and updates |
| `DELETE /api/models/{id}?files=true` | remove a model, optionally with its files |
| `POST /api/updates/check` | ask upstream for the newest versions now → job |
| `POST /api/updates/{component}` | build/install and switch → job. Body `{"version": "b10545"}` pins a tag |
| `POST /api/rollback/{component}` | back to the previous version → job |

**Putting a model on one card or on all of them** — exactly the question an agent
cannot otherwise answer from outside:

```bash
T=$(ssh <user>@<server-ip> llm api token)
curl -X PATCH http://<server-ip>:8081/api/models/qwen3.8-27b-q6_k \
     -H "X-LLM-Token: $T" -H 'Content-Type: application/json' \
     -d '{"gpu":"both"}'        # "both" | a card number
```

What happens: `--device ROCmN -sm none -mg 0` (and for MTP models
`--spec-draft-device ROCmN`) is set or removed, the GPU groups are regenerated, and
llama-swap restarts. Whisper models steer their card through `HIP_VISIBLE_DEVICES`,
which the same PATCH handles — translating the logical number you sent into the
absolute one that variable needs.

**It checks first whether the model fits**, and it checks **per card**, not as a sum:
with an even tensor split a 30 GB model needs 15 GB on *each* card, so plenty of free
space on one of them does not make it fit. If it does not, you get `409` in plain
words:

```
409 {"detail":"needs about 51.3 GB on all 2 cards, i.e. 25.6 GB per card -
     card 0 has only 3.3 GB free"}
```

`{"force": true}` overrides that.

> The fit check compares against the VRAM that is free **right now**, so patching a
> model while it is loaded fails against its own footprint. `POST /api/unload`
> first, or pass `?dryRun=true` to see the diff without the check biting.

**Changing the slot count** — how many requests a model serves at once:

```bash
curl -X PATCH http://<server-ip>:8081/api/models/qwen3.8-27b-q6_k \
     -H "X-LLM-Token: $T" -H 'Content-Type: application/json' \
     -d '{"parallel": 4}'
```

That writes `-np 4 -kvu` and removes whatever spelling was there before. Slots cost
no KV cache — `-c` is the total either way — but a little compute buffer, ~1.4 GB on
a 27B. What they buy is fairness rather than throughput; the measurements are in
[FLAGS.md](FLAGS.md#slots-several-clients-or-agents-at-once).

## Provenance: which quant from which publisher

Publishers genuinely differ (bartowski with imatrix, unsloth with UD quants,
mradermacher, ggml-org). So every model directory carries a
`models/<name>/.llm-model.json`:

```json
{ "repo": "bartowski/Qwen3.8-27B-GGUF", "revision": "f0eec4a4bb49…", "quant": "Q6_K",
  "files": [{ "name": "Qwen3.8-27B-Q6_K.gguf", "sizeBytes": 23463130720, "sha256": "7d59…" }],
  "source": "llm add", "verified": true, "addedAt": "2026-08-16T15:02:11Z" }
```

- `llm add` writes it automatically.
- For models that were already there: `llm meta backfill`. The commit hash from the
  Hugging Face cache is verified against `api/models/{repo}/revision/{sha}` — that is
  **proof**, not a guess (`verified: true`).
- `llm meta` shows the overview, `llm meta show <dir>` a single file.

### Which backend answered

`GET /api/versions`, `GET /api/state` and `GET /api/health` all carry
`"backend": "rocm" | "vulkan"`. It decides how everything else about the cards was
read:

- Under **rocm**, one `rocm-smi` query answers temperature, power, utilisation,
  VRAM, name and ISA target.
- Under **vulkan**, the cards come from `vulkaninfo` and the measurements from
  amdgpu's sysfs, joined by the DRM card number the Vulkan driver reports. On a
  card whose driver is not amdgpu those fields are **absent** rather than zero,
  and so is `gfx` — the model still runs, there is just no thermometer.
- `--device ROCmN` versus `--device VulkanN` follows the backend, and so does the
  visible-devices mask: `HIP_VISIBLE_DEVICES` or `GGML_VK_VISIBLE_DEVICES`. The
  `hipVisibleDevices` field in `GET /api/hw` keeps its name under both — renaming
  a documented field to say the same thing differently would break clients for
  nothing — and `visibleEnv` says which variable it is written under.

Reading a configuration accepts **either** spelling whatever is active, so a
config written under one backend keeps its card pinnings after a switch;
`llm gpu backend` rewrites them. And `check_fit` answers "the fit was not
checked" rather than refusing, when a card's free VRAM cannot be read at all.

## MCP (Claude Code and others)

The same service speaks MCP over `http://<server-ip>:8081/mcp` (streamable HTTP):

```bash
claude mcp add --transport http llm-box http://<server-ip>:8081/mcp \
  --header "X-LLM-Token: $(ssh <user>@<server-ip> llm api token)"
```

Tools: `list_models`, `get_model`, `gpu_status`, `set_model_config`, `load_model`,
`unload_models`, `add_model`, `remove_model`, `job_status`. Reading needs no token;
anything that changes state demands one and says exactly what is missing otherwise.

> **MCP answering `421 Misdirected Request`?** The MCP transport checks the `Host`
> header as protection against DNS rebinding. Allowed are `localhost`, `127.0.0.1`,
> the machine's hostname and its LAN addresses, each with and without port. If you
> reach the server under a different name (a VPN name, `*.local`, a reverse proxy),
> add it in the unit:
> `Environment=LLM_API_ALLOWED_HOSTS=myhost:8081,llm.your-tailnet.ts.net:8081` — or
> `*` to switch the check off. The plain HTTP API (`/api/...`) is unaffected.
>
> The `Origin` header is checked against the same list. It used to accept `*`,
> which left half of that protection open to a browser-driven request while the
> service answers with a session cookie. An MCP client that is not a browser
> sends no `Origin` at all and never notices.

pi does not need MCP — it has an extension, see [PI.md](PI.md).

### Nine tools over MCP, six in pi — on purpose

The registry exposes `list_models`, `get_model`, `gpu_status`,
`set_model_config`, `load_model`, `unload_models`, `add_model`, `remove_model`
and `job_status`. The pi extension registers six: it leaves out `add_model`,
`remove_model` and `job_status`.

That is not drift. Downloading 22 GB and deleting a model are in pi's
interactive `/llm` command instead, behind a confirmation, because an agent that
can delete a model on its own turn is a worse trade than one that has to ask.
What an agent connected over plain MCP can do is therefore *more* than what the
same agent can do through the pi extension — worth knowing before you wonder
which one is broken.

## Security

Reads are unauthenticated by default; writes need the token. If you do not want the
registry reachable at all: `llm api off` — the LLM API on 8080 keeps running, only
agents lose the catalog. See [../SECURITY.md](../SECURITY.md) for the full picture.

Input is validated before anything reaches the configuration: `repo` must be
`publisher/name`, `quant` alphanumeric, `extraFlags` must contain no shell
metacharacters, and `gpu` must be `both` or a card number that actually exists.
Otherwise `400`.

## Operating it

- **Firewall.** If `ufw` is active, port 8081 has to be open for your LAN, or nothing
  arrives from another machine — and it fails *silently*: the service runs, the log
  says nothing, because the packets never get there. `setup-system.sh` adds the rules;
  by hand:
  ```bash
  sudo ufw allow from <your-subnet> to any port 8081 proto tcp comment 'llm registry'
  sudo ufw status | grep 8081
  ```
  Note that a test **on the server itself** goes over `lo` and succeeds even with the
  firewall blocking — it proves nothing about reachability from outside.
- **Service:** `systemd/llm-api.service` (user unit, autostart). Logs: `llm api logs`.
  `llm on`/`llm off` include it, so after `llm off` the catalog is gone too — which is
  honest, since nothing could be running anyway.
- **Python environment:** the registry has its **own** `venv-api/` with just
  `fastapi`, `uvicorn` and `mcp` (~33 MB). It used to share the 6.4 GB environment
  with Open WebUI, which meant an Open WebUI upgrade could move `fastapi`/`pydantic`/
  `mcp` underneath it and take every agent offline. Note that `mcp` is pinned to the
  **2.x** series and cannot span both: 2.0 renamed `mcp.server.fastmcp` to
  `mcp.server.mcpserver`, dropped the module-wide `get_context()` in favour of a
  `Context` injected into each tool, and moved `stateless_http`,
  `streamable_http_path` and `transport_security` from the constructor to
  `streamable_http_app()`. An existing installation needs `llm setup` once to move
  its `venv-api`, and until it does the registry will not start.
- **During an update** (`llm update llama|swap`) llama-swap is briefly stopped. The
  registry keeps running, reports `swapUp: false` for that moment, and the catalog
  stays readable.
- **Concurrent changes:** `llm add`/`llm rm` on the server and a `PATCH` from outside
  take the same lock file (`config/.llama-swap.lock`), so no half-written
  configuration can happen regardless of who gets there first.
