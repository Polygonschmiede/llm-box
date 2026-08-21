#!/usr/bin/env python3
"""llm-api - the model registry as an endpoint (HTTP + MCP).

With this, EVERY agent - pi, Claude Code, a script - always sees the real state:
which models exist, how they are configured, where they came from and what is
And it can change them without logging in to this machine.

  Reading: no authentication on the local network
  Writing: header  X-LLM-Token: <contents of config/api-token>

Start:  venv-api/bin/python bin/llm-api.py                (or: llm api on)
Docs :  docs/API.md
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
#  MUST come before "import llmreg": llmreg evaluates LLM_HOME at import time,
#  so setting it afterwards would have no effect.
os.environ.setdefault("LLM_HOME", ROOT)

import anyio                                                            # noqa: E402
from fastapi import (                                                     # noqa: E402
    Body, Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response,
)
from fastapi.responses import (                                          # noqa: E402
    FileResponse, JSONResponse, StreamingResponse,
)

import llmreg                                                            # noqa: E402

#  Same source as bin/llm, so the CLI and the API can never disagree.
try:
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as _fh:
        LLM_BOX_VERSION = _fh.read().strip() or "unknown"
except OSError:
    LLM_BOX_VERSION = "unknown"

#  Loopback by default: started by hand, the service is not accidentally on the
#  network. The unit sets LLM_API_HOST explicitly, so the decision lives in one
#  greppable place.
HOST = os.environ.get("LLM_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("LLM_API_PORT", "8081"))
LLM_CLI = os.path.join(llmreg.LLM_HOME, "bin", "llm")


# ---------------------------------------------------------------------------
#  Catalog cache: re-read as soon as the configuration changes
# ---------------------------------------------------------------------------
class Catalog:
    def __init__(self):
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._static: list[dict] = []

    def _config_mtime(self) -> float:
        try:
            return os.path.getmtime(llmreg.CONFIG)
        except OSError:
            return 0.0

    def all(self) -> list[dict]:
        """The full catalog. The static part is cached, the live state never is."""
        mt = self._config_mtime()
        with self._lock:
            if mt != self._mtime or not self._static:
                try:
                    self._static = llmreg.catalog(with_live=False)
                except llmreg.ConfigMissing as exc:
                    #  A fresh checkout, not a server fault - say which command
                    #  fixes it instead of returning a traceback.
                    raise HTTPException(503, str(exc)) from None
                self._mtime = mt
            base = [json.loads(json.dumps(m)) for m in self._static]
        state = llmreg.live()
        running = {r.get("model"): r for r in state["running"]}
        for m in base:
            llmreg.recheck_files(m)          # never answer file questions from the cache
            m["state"] = state["states"].get(m["id"], "unknown" if not state["up"] else "unloaded")
            r = running.get(m["id"])
            if r:
                m["state"] = r.get("state", "ready")
                m["runtime"]["proxy"] = r.get("proxy")
        #  Roles are cached without live data, so their activeTargets would stay
        #  empty. Derive them from the targets we just refreshed - a role is
        #  "ready" exactly when one of its targets is, whatever llama-swap
        #  reports for the selector name itself.
        live_state = {m["id"]: m["state"] for m in base}
        for m in base:
            sel = m["runtime"].get("selector")
            if not sel:
                continue
            active = [t for t in sel["targets"]
                      if live_state.get(t) in ("ready", "loading")]
            m["activeTargets"] = active
            m["state"] = "ready" if active else "unloaded"
        return base

    def one(self, model_id: str) -> dict:
        for m in self.all():
            if m["id"] == model_id:
                return m
        raise HTTPException(404, "no such model: '%s'" % model_id)

    def invalidate(self):
        with self._lock:
            self._mtime = 0.0


CAT = Catalog()


# ---------------------------------------------------------------------------
#  Jobs (downloads take a long time)
# ---------------------------------------------------------------------------
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


#  bin/llm colours its output, and cmake/git/uv do too when they think they are
#  on a terminal. Those escapes would show up literally in the web page, so the
#  child is asked to keep quiet (NO_COLOR) and whatever still slips through is
#  stripped here.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def job_start(kind: str, argv: list[str], env: dict | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "argv": argv, "state": "running",
           "startedAt": time.time(), "log": [], "exitCode": None}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def run():
        e = dict(os.environ, HF_HUB_DISABLE_XET="1", NO_COLOR="1", **(env or {}))
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1, env=e)
            for line in p.stdout:                      # type: ignore[union-attr]
                line = ANSI.sub("", line.rstrip("\n"))
                with JOBS_LOCK:
                    job["log"].append(line)
                    del job["log"][:-400]              # keep only the last lines
            job["exitCode"] = p.wait()
        except Exception as exc:                       # noqa: BLE001
            job["log"].append("error: %s" % exc)
            job["exitCode"] = -1
        job["state"] = "done" if job["exitCode"] == 0 else "failed"
        job["finishedAt"] = time.time()
        CAT.invalidate()

    threading.Thread(target=run, daemon=True).start()
    return job_id


def job_running(*kinds: str) -> dict | None:
    """A running job of one of these kinds, or None.

    Updates need this: two of them would fetch into the same repository, move
    the same symlink and restart the same unit at the same time.
    """
    with JOBS_LOCK:
        for job in JOBS.values():
            if job["state"] == "running" and job["kind"] in kinds:
                return {k: v for k, v in job.items() if k != "log"}
    return None


# ---------------------------------------------------------------------------
#  Auth
# ---------------------------------------------------------------------------
#  Writes need the token from config/api-token, as a header or - so the web page
#  does not have to keep it in every form - as a session cookie obtained once
#  from POST /api/session. Sessions live in memory only: they are gone after a
#  restart, which is the right trade for something that guards one machine's
#  configuration and saves writing a session store.
SESSION_COOKIE = "llm_session"
SESSION_TTL = 12 * 3600
_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def _session_new() -> tuple[str, int]:
    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.time()
        for k, exp in list(_sessions.items()):        # opportunistic cleanup
            if exp < now:
                del _sessions[k]
        _sessions[sid] = now + SESSION_TTL
    return sid, SESSION_TTL


def _session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    with _sessions_lock:
        exp = _sessions.get(sid)
        if exp is None:
            return False
        if exp < time.time():
            del _sessions[sid]
            return False
    return True


def _token_ok(given: str | None, want: str | None) -> bool:
    """Constant-time token comparison.

    `==` on a secret leaks its length and its matching prefix through timing.
    Over a LAN that is a thin channel, but this is the only comparison in the
    project that guards anything, and it is one function call.
    """
    if not given or not want:
        return False
    return secrets.compare_digest(given, want)


def require_token(x_llm_token: str | None = Header(default=None),
                  llm_session: str | None = Cookie(default=None)):
    want = llmreg.api_token(create=False)
    if not want:
        raise HTTPException(503, "no token configured - run 'llm api token' on the server")
    if _token_ok(x_llm_token, want) or _session_valid(llm_session):
        return True
    raise HTTPException(401, "wrong or missing X-LLM-Token (or expired session)")


WRITE = [Depends(require_token)]

#  Reads are open by default, and that is deliberate rather than an oversight:
#  the pi extension fetches /api/models, /api/gpus, /api/health and /api/jobs
#  without a token, and docs/PI.md tells people the token is only for changes.
#  Requiring auth for reads would break every existing client, so it is opt-in.
#  Worth turning on if 8081 is reachable beyond your own machine: reads expose
#  every model path, checksum and Hugging Face repo, plus live VRAM per card.
READ_NEEDS_AUTH = os.environ.get("LLM_API_REQUIRE_AUTH", "").strip().lower() \
    in ("1", "true", "yes")


def require_read(x_llm_token: str | None = Header(default=None),
                 llm_session: str | None = Cookie(default=None)):
    if not READ_NEEDS_AUTH:
        return True
    return require_token(x_llm_token, llm_session)


READ = [Depends(require_read)]


# ---------------------------------------------------------------------------
#  MCP
# ---------------------------------------------------------------------------
from mcp.server.mcpserver import MCPServer, Context                     # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings     # noqa: E402

#  The MCP transport rejects unknown Host headers (DNS-rebinding protection) and
#  answers 421 otherwise - but this service is used precisely from OTHER machines
#  on the network. So allow our own addresses; LLM_API_ALLOWED_HOSTS
#  (comma-separated, "*" = any) adjusts the list.
def _allowed_hosts() -> list[str]:
    env = os.environ.get("LLM_API_ALLOWED_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    hosts = ["localhost", "127.0.0.1", socket.gethostname()]
    try:
        hosts += subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                                timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for h in hosts:
        out += [h, "%s:%d" % (h, PORT)]                 # with and without the port
    return out


#  Origins get the same list as hosts rather than "*". The Host check is the
#  DNS-rebinding protection; leaving Origin open kept half the door ajar for a
#  browser-driven request, and this service answers with a session cookie. An
#  MCP client that is not a browser sends no Origin at all and is unaffected -
#  LLM_API_ALLOWED_HOSTS widens both if you need it to.
mcp = MCPServer("llm-box")

#  Built here, not at the mount below: 'mcp.session_manager' raises until
#  streamable_http_app() has run once, and the lifespan needs it. Keeping the two
#  next to each other means the order cannot be broken by moving the mount.
#  In mcp 1.x all three of these were constructor arguments.
MCP_APP = mcp.streamable_http_app(
    streamable_http_path="/mcp", stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(),
        allowed_origins=_allowed_hosts()))


#  mcp 1.x had a module-wide mcp.get_context(); 2.0 injects the context into any
#  tool that asks for it by type hint, so the header has to be handed down. Every
#  tool below therefore takes a keyword-only 'ctx' - it stays out of the tool
#  schema, so nothing changes for a client.
def _mcp_token(ctx: Context | None) -> str | None:
    try:
        #  None on stdio and on any transport without a request object.
        headers = ctx.headers if ctx else None
        return headers.get("x-llm-token") if headers else None
    except Exception:                                                   # noqa: BLE001
        return None


def _mcp_check_token(ctx: Context | None):
    want = llmreg.api_token(create=False)
    if not want or not _token_ok(_mcp_token(ctx), want):
        raise ValueError("this action needs the X-LLM-Token header "
                         "(contents of config/api-token on the server).")


def _mcp_check_read(ctx: Context | None):
    """The read half of the MCP surface, gated the same way HTTP reads are.

    Without this, LLM_API_REQUIRE_AUTH closed /api/models over HTTP and left
    list_models, get_model, gpu_status and job_status wide open over MCP - the
    same catalog, the same filesystem paths, through a different door. Whoever
    sets that variable means "reads need a token", not "reads need a token
    unless you ask in MCP".
    """
    if READ_NEEDS_AUTH:
        _mcp_check_token(ctx)


async def _thread(fn, *a, **kw):
    return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))


def _slim(m: dict) -> dict:
    """Compact view for agents - everything that matters, without the byte desert."""
    gpu = m["runtime"]["gpu"]
    src = m.get("source") or {}
    sel = m["runtime"].get("selector")
    if sel:
        #  A role has no file, no card and no VRAM of its own. Reporting zeros
        #  for those would read as "a 0 GB model", so it gets its own shape.
        return {
            "id": m["id"], "kind": "role", "role": m["role"], "state": m["state"],
            "contextWindow": m["runtime"]["contextWindow"],
            "strategy": sel["strategy"], "targets": sel["targets"],
            "spillover": sel.get("spillover"),
            "activeTargets": m.get("activeTargets") or [],
            "vision": m["capabilities"]["vision"], "tools": m["capabilities"]["tools"],
            "reasoning": m["capabilities"]["reasoning"],
            "endpoint": m["endpoints"]["base"] + m["endpoints"]["path"],
            **({"description": m["description"]} if m.get("description") else {}),
        }
    return {
        "kind": "model",
        "id": m["id"], "role": m["role"], "state": m["state"],
        "contextWindow": m["runtime"]["contextWindow"],
        #  The string is for humans; gpuMode/gpuDevice exist so agents do not
        #  have to take prose apart.
        "gpu": "all cards" if gpu["mode"] == "both" else "card %s only" % gpu["device"],
        "gpuMode": gpu["mode"], "gpuDevice": gpu["device"],
        "vision": m["capabilities"]["vision"], "tools": m["capabilities"]["tools"],
        "reasoning": m["capabilities"]["reasoning"],
        "specDecoding": m["runtime"].get("specDecoding"),
        "sizeGB": round(((m.get("vram") or {}).get("weightsBytes") or 0) / 2**30, 1),
        "vramNeededGB": round(((m.get("vram") or {}).get("estimatedBytes") or 0) / 2**30, 1),
        "source": {"repo": src.get("repo"), "quant": src.get("quant"),
                   "revision": (src.get("revision") or "")[:12] or None,
                   "verified": src.get("verified")},
        "endpoint": m["endpoints"]["base"] + m["endpoints"]["path"],
        **({"issues": m["issues"]} if m.get("issues") else {}),
    }


@mcp.tool(description="All local models with their configuration, GPU placement and "
                      "Hugging Face provenance. Always the real, current state.")
async def list_models(role: str | None = None, *, ctx: Context) -> list[dict]:
    _mcp_check_read(ctx)
    models = await _thread(CAT.all)
    return [_slim(m) for m in models if not role or m["role"] == role]


@mcp.tool(description="Every detail of one model including the full llama-server "
                      "command line, its files and its VRAM requirement.")
async def get_model(model_id: str, *, ctx: Context) -> dict:
    _mcp_check_read(ctx)
    return await _thread(CAT.one, model_id)


@mcp.tool(description="VRAM and temperature per card, plus which models are pinned "
                      "to which card. The card count depends on the machine, so "
                      "call this before pinning anything.")
async def gpu_status(*, ctx: Context) -> list[dict]:
    _mcp_check_read(ctx)
    return await _thread(llmreg.gpus)


@mcp.tool(description="Change a model's configuration: gpu (a card number from 0, "
                      "or 'both' for all of them - gpu_status lists the cards), "
                      "context_window, slots (how many requests it serves at the "
                      "same time - raise this when several agents share one model), "
                      "ttl, temperature/top_p/top_k/min_p. "
                      "Checks that it fits in VRAM first. Needs X-LLM-Token.")
async def set_model_config(model_id: str, gpu: str | None = None,
                           context_window: int | None = None, slots: int | None = None,
                           ttl: int | None = None,
                           temperature: float | None = None, top_p: float | None = None,
                           top_k: int | None = None, min_p: float | None = None,
                           force: bool = False, dry_run: bool = False,
                           *, ctx: Context) -> dict:
    _mcp_check_token(ctx)
    sampling = {k: v for k, v in (("temperature", temperature), ("top_p", top_p),
                                  ("top_k", top_k), ("min_p", min_p)) if v is not None}
    changes = {"gpu": gpu, "contextWindow": context_window, "parallel": slots,
               "ttl": ttl, "sampling": sampling, "force": force}
    try:
        out = await _thread(llmreg.patch_model, model_id, changes, dry_run)
    except MemoryError as exc:
        raise ValueError("does not fit: %s. Use force=true to set it anyway." % exc) from exc
    CAT.invalidate()
    return out


@mcp.tool(description="Load a model into VRAM (llama-swap starts it).")
async def load_model(model_id: str, *, ctx: Context) -> dict:
    _mcp_check_token(ctx)
    return await _thread(llmreg.load_model, model_id)


@mcp.tool(description="Drop every loaded model out of VRAM.")
async def unload_models(*, ctx: Context) -> dict:
    _mcp_check_token(ctx)
    return await _thread(llmreg.unload_all)


@mcp.tool(description="Fetch a new GGUF model from Hugging Face and configure it. "
                      "Runs as a background job (downloads take a while); follow "
                      "progress with job_status. Needs X-LLM-Token.")
async def add_model(repo: str, quant: str = "Q4_K_M", gpu: str | None = None,
                    context_window: int | None = None, mtp: bool = False,
                    ngram: bool = False, ttl: int | None = None,
                    *, ctx: Context) -> dict:
    _mcp_check_token(ctx)
    body = {"repo": repo, "quant": quant, "gpu": gpu, "contextWindow": context_window,
            "mtp": mtp, "ngram": ngram, "ttl": ttl}
    try:
        return await _thread(lambda: api_add(body))
    except HTTPException as exc:
        raise ValueError(exc.detail) from exc


@mcp.tool(description="Remove a model from the configuration. delete_files=true "
                      "also deletes the GGUF files (irreversibly). "
                      "Needs X-LLM-Token.")
async def remove_model(model_id: str, delete_files: bool = False,
                       *, ctx: Context) -> dict:
    _mcp_check_token(ctx)
    return await _thread(llmreg.remove_model, model_id, delete_files)


@mcp.tool(description="State and log of a background job (e.g. a download).")
async def job_status(job_id: str, *, ctx: Context) -> dict:
    #  Job logs carry the full build and download output, including the argv the
    #  job was started with - so they are a read like any other.
    _mcp_check_read(ctx)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise ValueError("unknown job '%s'" % job_id)
    return {k: (v[-30:] if k == "log" else v) for k, v in job.items()}


# ---------------------------------------------------------------------------
#  HTTP-API
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    #  Create the write token here rather than under __main__: started through an
    #  ASGI server directly (uvicorn bin.llm-api:app, gunicorn, a reload wrapper)
    #  that branch never runs, and then every write answers 503 "no token
    #  configured" while 'llm api token' shows one - because the CLI creates it
    #  and the service did not.
    llmreg.api_token(create=True)
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="llm-box registry", version=LLM_BOX_VERSION, lifespan=lifespan,
              description="Model registry of the local LLM server")


#  ConfigMissing is not a server fault, it is the state of every clone before
#  'llm init'. Five routes converted it to 503 by hand and the rest did not, so
#  GET /api/gpus - which the control page fetches on every load - answered 500
#  with a traceback on a fresh checkout while /api/models answered 503 with the
#  command that fixes it. One handler covers every route, including the ones
#  nobody thought to wrap.
@app.exception_handler(llmreg.ConfigMissing)
def _config_missing(_request: Request, exc: llmreg.ConfigMissing):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/api/health")
def health():
    st = llmreg.live()
    models = CAT.all()
    problems = {m["id"]: m["issues"] for m in models if m.get("issues")}
    return {"ok": True, "swapUp": st["up"], "models": len(models),
            "problems": problems,          # missing files, unknown provenance
            "running": [r.get("model") for r in st["running"]],
            "versions": versions(), "publicApi": llmreg.PUBLIC_API,
            "backend": llmreg.backend_name(),
            "writeNeedsToken": bool(llmreg.api_token(create=False))}


def versions() -> dict:
    def out(*argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    swap = out(os.path.join(llmreg.LLM_HOME, "bin", "llama-swap"), "--version")
    lcpp = os.path.realpath(os.path.join(llmreg.LLM_HOME, "llama.cpp", "build"))
    return {"llmBox": LLM_BOX_VERSION,
            "llamaSwap": (swap or "").replace("version: ", "").split("\n")[0] or None,
            "llamaCpp": os.path.basename(lcpp).replace("build-", "") if lcpp else None}


@app.get("/api/models", dependencies=READ)
def api_models(role: str | None = Query(default=None),
               slim: bool = Query(default=False)):
    models = [m for m in CAT.all() if not role or m["role"] == role]
    return [_slim(m) for m in models] if slim else models


@app.get("/api/models/{model_id}", dependencies=READ)
def api_model(model_id: str):
    return CAT.one(model_id)


@app.get("/api/gpus", dependencies=READ)
def api_gpus():
    return llmreg.gpus()


@app.get("/api/state", dependencies=READ)
def api_state():
    st = llmreg.live()
    #  backend, because everything on the Cards tab is read differently under
    #  each one - and a card list with no temperatures should say "Vulkan on a
    #  driver that does not report them" rather than look broken.
    return {"swapUp": st["up"], "running": st["running"], "states": st["states"],
            "backend": llmreg.backend_name(), "gpus": llmreg.gpus()}


@app.get("/api/pi-models.json")
def api_pi_models(x_llm_token: str | None = Header(default=None),
                  llm_session: str | None = Cookie(default=None)):
    """The provider block pi consumes. Open, EXCEPT when it carries a secret.

    This one endpoint stays reachable without a token so a client can bootstrap
    itself - but once an inference key is set (llm key new) the payload contains
    it, and handing that to anyone who can reach the port would undo the whole
    point of setting it. So the rule follows the content: no key, no
    authentication; key, token or session required.
    """
    if llmreg.api_key():
        want = llmreg.api_token(create=False)
        if not (want and (_token_ok(x_llm_token, want) or _session_valid(llm_session))):
            raise HTTPException(401, "an inference key is set, so this payload carries a "
                                     "secret - send X-LLM-Token (llm api token)")
    return llmreg.pi_models_json(CAT.all())


@app.get("/api/events", dependencies=READ)
async def api_events():
    """SSE: announces every change to the configuration or the load state."""
    async def gen():
        last: dict | None = None
        while True:
            snap = {"mtime": os.path.getmtime(llmreg.CONFIG),
                    "states": (await _thread(llmreg.live))["states"]}
            if snap != last:
                if last is not None and snap["mtime"] != last["mtime"]:
                    CAT.invalidate()
                last = snap
                yield "data: %s\n\n" % json.dumps(
                    {"type": "state", "states": snap["states"], "configMtime": snap["mtime"]})
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/session")
def api_session(response: Response, body: dict = Body(default={})):
    """Exchange the token for a session cookie, so the web page holds no secret.

    The cookie is HttpOnly and SameSite=Strict. It is NOT marked Secure: this
    project serves plain HTTP, and setting the flag would make the cookie
    unusable rather than the connection safe. Over an untrusted network the
    answer is an SSH tunnel or a TLS proxy, not a flag - see SECURITY.md.
    """
    want = llmreg.api_token(create=False)
    if not want:
        raise HTTPException(503, "no token configured - run 'llm api token' on the server")
    if not _token_ok(str(body.get("token") or ""), want):
        raise HTTPException(401, "wrong token")
    sid, ttl = _session_new()
    response.set_cookie(SESSION_COOKIE, sid, max_age=ttl, httponly=True,
                        samesite="strict", path="/")
    return {"ok": True, "expiresInSeconds": ttl}


@app.delete("/api/session")
def api_session_end(response: Response, llm_session: str | None = Cookie(default=None)):
    if llm_session:
        with _sessions_lock:
            _sessions.pop(llm_session, None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/session")
def api_session_state(llm_session: str | None = Cookie(default=None),
                      x_llm_token: str | None = Header(default=None)):
    """Whether this caller may write. The page asks before showing any button."""
    want = llmreg.api_token(create=False)
    return {"canWrite": bool(want) and (_token_ok(x_llm_token, want)
                                       or _session_valid(llm_session)),
            "tokenConfigured": bool(want), "readNeedsAuth": READ_NEEDS_AUTH}


@app.get("/api/versions", dependencies=READ)
def api_versions():
    """Engine versions, what is installed alongside and how to roll back.

    Only `llm versions` and `llm status` knew any of this, so nothing outside
    the machine could tell whether an update was pending or what there was to
    fall back to. 'latest' comes from .update-cache and never from a live
    GitHub call - the same rule the CLI follows, so neither blocks on the
    network.
    """
    return llmreg.engine_versions()


@app.get("/api/config", dependencies=READ)
def api_config():
    """The parts of llama-swap.yaml that are not per-model: macros and groups.

    llama-swap's own UI has no /api/config at all, so the eviction semantics -
    swap, exclusive, persistent - were visible only by opening the file.
    """
    return llmreg.config_overview()


@app.get("/api/config/diff", dependencies=READ)
def api_config_diff():
    """What `llm gpu sync` would change. Empty diff = configuration matches."""
    out = llmreg.gpu_sync(dry_run=True)
    return {"diff": out.get("diff") or "", "cards": out.get("cards"),
            "tensorSplit": out.get("tensorSplit"),
            "drift": llmreg.tensor_split_drift()}


# ---------------------------------------------------------------------------
#  Roles. Until now these could only be created from the CLI, which meant a UI
#  or a remote agent could see them and not change them.
# ---------------------------------------------------------------------------
@app.get("/api/roles", dependencies=READ)
def api_roles():
    return llmreg.read_selectors()


@app.put("/api/roles/{name}", dependencies=WRITE)
def api_role_put(name: str, body: dict = Body(...),
                 dry_run: bool = Query(default=False, alias="dryRun")):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise HTTPException(400, "a role name may hold letters, digits, dot, dash "
                                 "and underscore, and must start with one of the first two")
    targets = body.get("targets")
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        raise HTTPException(400, "targets must be a list of model names")
    spill = body.get("spillover")
    if spill is not None and not (isinstance(spill, int) and 1 <= spill <= 64):
        raise HTTPException(400, "spillover must be a whole number 1..64")
    try:
        out = llmreg.set_selector(name, str(body.get("strategy") or ""), targets,
                                  spillover=spill, description=body.get("description"),
                                  dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    CAT.invalidate()
    if not dry_run:
        out["reloaded"] = llmreg.reload_swap()
    return out


@app.delete("/api/roles/{name}", dependencies=WRITE)
def api_role_delete(name: str):
    try:
        out = llmreg.del_selector(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from None
    CAT.invalidate()
    out["reloaded"] = llmreg.reload_swap()
    return out


@app.patch("/api/models/{model_id}", dependencies=WRITE)
def api_patch(model_id: str, body: dict = Body(default={}),
              dry_run: bool = Query(default=False, alias="dryRun")):
    CAT.one(model_id)                                  # 404 if it does not exist
    _check_patch(body)
    try:
        out = llmreg.patch_model(model_id, body, dry_run=dry_run)
    except MemoryError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    CAT.invalidate()
    return out


@app.post("/api/models/{model_id}/load", dependencies=WRITE)
def api_load(model_id: str):
    CAT.one(model_id)
    try:
        return llmreg.load_model(model_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/unload", dependencies=WRITE)
def api_unload_all():
    """llama-swap only knows "unload everything" - hence no model id here."""
    return llmreg.unload_all()


@app.post("/api/models/{model_id}/unload", dependencies=WRITE)
def api_unload(model_id: str):
    CAT.one(model_id)
    out = llmreg.unload_all()
    out["hint"] = "llama-swap always unloads every model (see POST /api/unload)"
    return out


@app.delete("/api/models/{model_id}", dependencies=WRITE)
def api_delete(model_id: str, files: bool = Query(default=False)):
    CAT.one(model_id)
    out = llmreg.remove_model(model_id, delete_files=files)
    CAT.invalidate()
    return out


def _check_gpu(gpu) -> None:
    """A card number that exists, or 'both'. Used by POST *and* PATCH.

    PATCH used to skip this entirely, so a model could be pinned to a card the
    machine does not have - llama-server then failed at load time with nothing
    pointing back at the request that caused it.
    """
    if gpu is None or str(gpu) in ("both", "all"):
        return
    n = llmreg.gpu_count()
    if not re.fullmatch(r"\d+", str(gpu)) or (n and int(gpu) >= n):
        raise HTTPException(400, "gpu must be 'both' or a card number 0..%d "
                                 "(detected: %d card(s), see GET /api/gpus)"
                                 % (max(n - 1, 0), n))


def _check_patch(body: dict) -> None:
    """What PATCH accepts. Mirrors the checks POST /api/models already had."""
    _check_gpu(body.get("gpu"))
    for key in ("ttl", "contextWindow"):
        v = body.get(key)
        if v is not None and not (isinstance(v, int) and 0 <= v <= 10_000_000):
            raise HTTPException(400, "%s must be a whole number" % key)
    np_ = body.get("parallel")
    if np_ is not None and not (isinstance(np_, int) and 1 <= np_ <= 64):
        raise HTTPException(400, "parallel must be a whole number 1..64")


def _check_add(body: dict) -> tuple[str, str]:
    """Validate input: these flags end up in the cmd line llama-swap executes."""
    repo = str(body.get("repo") or "")
    quant = str(body.get("quant") or "Q4_K_M")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        raise HTTPException(400, "repo must be 'publisher/name' (e.g. 'unsloth/Qwen3-8B-GGUF')")
    if not re.fullmatch(r"[A-Za-z0-9_.]+", quant):
        raise HTTPException(400, "quant contains characters that are not allowed "
                                 "(it looks like 'Q4_K_M')")
    extra = str(body.get("extraFlags") or "")
    if re.search(r"[;&|<>`$\n\r\"']", extra):
        raise HTTPException(400, "extraFlags contains metacharacters - plain flags only")
    _check_gpu(body.get("gpu"))
    for key in ("ttl", "contextWindow"):
        v = body.get(key)
        if v is not None and not (isinstance(v, int) and 0 <= v <= 10_000_000):
            raise HTTPException(400, "%s must be a whole number" % key)
    slots = body.get("slots")
    if slots is not None and not (isinstance(slots, int) and 1 <= slots <= 64):
        raise HTTPException(400, "slots must be a whole number 1..64")
    return repo, quant


@app.post("/api/models", dependencies=WRITE, status_code=202)
def api_add(body: dict = Body(...)):
    repo, quant = _check_add(body)
    body = dict(body, quant=quant)
    argv = [LLM_CLI, "add"]
    if body.get("mtp"):
        argv.append("--mtp")
    if body.get("ngram"):
        argv.append("--ngram")
    if body.get("embed"):
        argv.append("--embed")
    if body.get("rerank"):
        argv.append("--rerank")
    if body.get("gpu") not in (None, "both"):
        argv += ["--gpu", str(body["gpu"])]
    if body.get("slots") is not None:
        argv += ["--slots", str(body["slots"])]
    if body.get("ttl") is not None:
        argv += ["--ttl", str(body["ttl"])]
    if body.get("contextWindow"):
        argv += ["-c", str(body["contextWindow"])]
    argv += [repo, quant]
    if body.get("extraFlags"):
        argv += ["--", *str(body["extraFlags"]).split()]
    job_id = job_start("add", argv)
    return {"jobId": job_id, "argv": argv,
            "hint": "progress: GET /api/jobs/%s" % job_id}


@app.get("/api/jobs", dependencies=READ)
def api_jobs():
    with JOBS_LOCK:
        return [{k: v for k, v in j.items() if k != "log"} for j in JOBS.values()]


@app.get("/api/jobs/{job_id}", dependencies=READ)
def api_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


# ---------------------------------------------------------------------------
#  Updates and rollbacks
# ---------------------------------------------------------------------------
#  The machinery stays in lib/update.sh - this only starts 'llm update' /
#  'llm rollback' through the same job runner the downloads use, so the control
#  page can follow along instead of telling people to open a shell.
#
#  An allowlist rather than a pattern: these strings become a command line, and
#  a path parameter must never be able to contribute a word of its own.
UPDATE_COMPONENTS = ("llama", "swap", "whisper", "ui", "comfy")


def _check_component(component: str, allow_all: bool) -> str:
    known = UPDATE_COMPONENTS + (("all",) if allow_all else ())
    if component not in known:
        raise HTTPException(400, "component must be one of: %s" % ", ".join(known))
    return component


def _check_version(body: dict | None) -> str | None:
    """An explicit target tag, as 'llm update llama b10516' takes one."""
    version = (body or {}).get("version")
    if version in (None, ""):
        return None
    version = str(version)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version):
        raise HTTPException(400, "version must be a tag such as 'b10545' or 'v1.9.3'")
    return version


def _start_update(kind: str, argv: list[str]) -> dict:
    #  One at a time: two of these would fetch into the same repository, move the
    #  same symlink and restart the same unit at once.
    busy = job_running("update", "rollback", "check")
    if busy:
        raise HTTPException(409, "'%s' is still running (job %s) - these have to "
                                 "take turns, they share the repositories and the "
                                 "services" % (" ".join(busy["argv"][1:]), busy["id"]))
    job_id = job_start(kind, argv)
    return {"jobId": job_id, "argv": argv[1:],
            "hint": "progress: GET /api/jobs/%s" % job_id}


@app.post("/api/updates/check", dependencies=WRITE, status_code=202)
def api_update_check():
    """Ask upstream for the newest versions, now.

    Registered before /api/updates/{component} on purpose - the other route
    would swallow this path. .update-cache is up to a day old (UPD_MAXAGE in
    lib/update.sh) and until now only the CLI could refresh it, so the version
    table could show a stale 'latest' with no way to correct it from here.
    'llm update status' does the querying and prints what it found.
    """
    return _start_update("check", [LLM_CLI, "update", "status"])


@app.post("/api/updates/{component}", dependencies=WRITE, status_code=202)
def api_update(component: str, body: dict | None = Body(default=None)):
    """Build or install a component and switch to it. 'all' does every one.

    This takes a while - a llama.cpp build is tens of minutes - and llama-swap
    is down for a few seconds while the symlink moves. A smoke test decides
    whether the new version becomes active at all; when it fails, the running
    one stays. The full build output goes to a log file on the server, the job
    log carries the progress lines.
    """
    component = _check_component(component, allow_all=True)
    version = _check_version(body)
    argv = [LLM_CLI, "update", component] + ([version] if version else [])
    return _start_update("update", argv)


@app.post("/api/rollback/{component}", dependencies=WRITE, status_code=202)
def api_rollback(component: str):
    """Back to the previous version. /api/versions says whether there is one."""
    component = _check_component(component, allow_all=False)
    return _start_update("rollback", [LLM_CLI, "rollback", component])


WEB_PAGE = os.path.join(ROOT, "web", "index.html")


@app.get("/ui")
@app.get("/ui/")
def api_ui():
    """The control page. One file, no build step, served from the same origin as
    the API so there is nothing to configure for CORS.

    Registered before the MCP mount on "/" deliberately; a route added after it
    would never be reached.
    """
    if not os.path.exists(WEB_PAGE):
        raise HTTPException(404, "web/index.html is missing from this checkout")
    #  no-store: the page is small and always reflects a live configuration.
    return FileResponse(WEB_PAGE, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


#  The page's stylesheets. A table rather than a static mount: this process reads
#  every model file and config/api-token, and a path parameter that reaches the
#  filesystem is how that turns into someone else's shell. The names are what the
#  page asks for; the paths are ours.
UI_ASSETS = {
    "stellar.css": os.path.join(ROOT, "web", "vendor", "stellar", "index.css"),
    "stellar-auto-dark.css": os.path.join(ROOT, "web", "vendor", "stellar", "auto-dark.css"),
}


@app.get("/ui/{asset}")
def api_ui_asset(asset: str, if_none_match: str | None = Header(default=None)):
    """One of the page's stylesheets, by name.

    Registered before the MCP mount for the same reason /ui is.

    'no-cache' rather than 'no-store', plus the conditional check by hand:
    FileResponse sends an ETag but does not answer If-None-Match itself (that
    lives in StaticFiles, which this deliberately is not), so without these four
    lines the browser would re-fetch 78 KB of unchanged CSS on every page load.
    A long max-age is not the answer either - the URL stays the same across
    versions, so it would serve the old design system after an upgrade.
    """
    path = UI_ASSETS.get(asset)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "no such asset - the page asks for %s"
                                 % ", ".join(sorted(UI_ASSETS)))
    #  stat_result eagerly: without it FileResponse only computes the ETag while
    #  it is being sent, and there would be nothing here to compare against.
    r = FileResponse(path, media_type="text/css; charset=utf-8",
                     stat_result=os.stat(path),
                     headers={"Cache-Control": "no-cache"})
    #  Its own ETag, so the two can never be computed differently.
    tag = r.headers.get("etag")
    if tag and if_none_match and tag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers={"ETag": tag,
                                                 "Cache-Control": "no-cache"})
    return r


@app.get("/")
def root():
    return JSONResponse({
        "service": "llm-box registry",
        "version": LLM_BOX_VERSION,
        "ui": "/ui",
        "read": ["/api/health", "/api/models", "/api/models/{id}", "/api/gpus",
                 "/api/state", "/api/versions", "/api/config", "/api/config/diff",
                 "/api/roles", "/api/jobs", "/api/pi-models.json", "/api/events",
                 "/ui/{asset}"],
        "write": ["PATCH /api/models/{id}", "POST /api/models",
                  "POST /api/models/{id}/load", "POST /api/unload",
                  "DELETE /api/models/{id}", "PUT /api/roles/{name}",
                  "DELETE /api/roles/{name}", "POST /api/updates/check",
                  "POST /api/updates/{component}", "POST /api/rollback/{component}"],
        "mcp": "/mcp",
        "docs": "/docs",
        "hint": "writing needs the X-LLM-Token header, or a session cookie from "
                "POST /api/session",
        "readNeedsAuth": READ_NEEDS_AUTH,
    })


# Deliberately mounted at "/" (the MCP app brings its own /mcp path): mounting on
# "/mcp" would redirect POSTs to /mcp/ with a 307, which some MCP clients cannot
# follow. All /api routes are registered before this one and therefore still win.
app.mount("/", MCP_APP)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
