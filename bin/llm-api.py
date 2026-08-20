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
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse            # noqa: E402

import llmreg                                                            # noqa: E402

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


def job_start(kind: str, argv: list[str], env: dict | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "argv": argv, "state": "running",
           "startedAt": time.time(), "log": [], "exitCode": None}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def run():
        e = dict(os.environ, HF_HUB_DISABLE_XET="1", **(env or {}))
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1, env=e)
            for line in p.stdout:                      # type: ignore[union-attr]
                line = line.rstrip("\n")
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


# ---------------------------------------------------------------------------
#  Auth
# ---------------------------------------------------------------------------
def require_token(x_llm_token: str | None = Header(default=None)):
    want = llmreg.api_token(create=False)
    if not want:
        raise HTTPException(503, "no token configured - run 'llm api token' on the server")
    if x_llm_token != want:
        raise HTTPException(401, "wrong or missing X-LLM-Token")
    return True


WRITE = [Depends(require_token)]


# ---------------------------------------------------------------------------
#  MCP
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP                                  # noqa: E402
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


mcp = FastMCP("llm-box", stateless_http=True, streamable_http_path="/mcp",
              transport_security=TransportSecuritySettings(
                  allowed_hosts=_allowed_hosts(),
                  allowed_origins=["*"]))


def _mcp_token() -> str | None:
    try:
        req = mcp.get_context().request_context.request
        return req.headers.get("x-llm-token") if req else None
    except Exception:                                                   # noqa: BLE001
        return None


def _mcp_check_token():
    want = llmreg.api_token(create=False)
    if not want or _mcp_token() != want:
        raise ValueError("this action needs the X-LLM-Token header "
                         "(contents of config/api-token on the server).")


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
async def list_models(role: str | None = None) -> list[dict]:
    models = await _thread(CAT.all)
    return [_slim(m) for m in models if not role or m["role"] == role]


@mcp.tool(description="Every detail of one model including the full llama-server "
                      "command line, its files and its VRAM requirement.")
async def get_model(model_id: str) -> dict:
    return await _thread(CAT.one, model_id)


@mcp.tool(description="VRAM and temperature per card, plus which models are pinned "
                      "to which card. The card count depends on the machine, so "
                      "call this before pinning anything.")
async def gpu_status() -> list[dict]:
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
                           force: bool = False, dry_run: bool = False) -> dict:
    _mcp_check_token()
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
async def load_model(model_id: str) -> dict:
    _mcp_check_token()
    return await _thread(llmreg.load_model, model_id)


@mcp.tool(description="Drop every loaded model out of VRAM.")
async def unload_models() -> dict:
    _mcp_check_token()
    return await _thread(llmreg.unload_all)


@mcp.tool(description="Fetch a new GGUF model from Hugging Face and configure it. "
                      "Runs as a background job (downloads take a while); follow "
                      "progress with job_status. Needs X-LLM-Token.")
async def add_model(repo: str, quant: str = "Q4_K_M", gpu: str | None = None,
                    context_window: int | None = None, mtp: bool = False,
                    ngram: bool = False, ttl: int | None = None) -> dict:
    _mcp_check_token()
    body = {"repo": repo, "quant": quant, "gpu": gpu, "contextWindow": context_window,
            "mtp": mtp, "ngram": ngram, "ttl": ttl}
    try:
        return await _thread(lambda: api_add(body))
    except HTTPException as exc:
        raise ValueError(exc.detail) from exc


@mcp.tool(description="Remove a model from the configuration. delete_files=true "
                      "also deletes the GGUF files (irreversibly). "
                      "Needs X-LLM-Token.")
async def remove_model(model_id: str, delete_files: bool = False) -> dict:
    _mcp_check_token()
    return await _thread(llmreg.remove_model, model_id, delete_files)


@mcp.tool(description="State and log of a background job (e.g. a download).")
async def job_status(job_id: str) -> dict:
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
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="llm-box registry", version="1.0", lifespan=lifespan,
              description="Model registry of the local LLM server")


@app.get("/api/health")
def health():
    st = llmreg.live()
    models = CAT.all()
    problems = {m["id"]: m["issues"] for m in models if m.get("issues")}
    return {"ok": True, "swapUp": st["up"], "models": len(models),
            "problems": problems,          # missing files, unknown provenance
            "running": [r.get("model") for r in st["running"]],
            "versions": versions(), "publicApi": llmreg.PUBLIC_API,
            "writeNeedsToken": bool(llmreg.api_token(create=False))}


def versions() -> dict:
    def out(*argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    swap = out(os.path.join(llmreg.LLM_HOME, "bin", "llama-swap"), "--version")
    lcpp = os.path.realpath(os.path.join(llmreg.LLM_HOME, "llama.cpp", "build"))
    return {"llamaSwap": (swap or "").replace("version: ", "").split("\n")[0] or None,
            "llamaCpp": os.path.basename(lcpp).replace("build-", "") if lcpp else None}


@app.get("/api/models")
def api_models(role: str | None = Query(default=None),
               slim: bool = Query(default=False)):
    models = [m for m in CAT.all() if not role or m["role"] == role]
    return [_slim(m) for m in models] if slim else models


@app.get("/api/models/{model_id}")
def api_model(model_id: str):
    return CAT.one(model_id)


@app.get("/api/gpus")
def api_gpus():
    return llmreg.gpus()


@app.get("/api/state")
def api_state():
    st = llmreg.live()
    return {"swapUp": st["up"], "running": st["running"],
            "states": st["states"], "gpus": llmreg.gpus()}


@app.get("/api/pi-models.json")
def api_pi_models():
    return llmreg.pi_models_json(CAT.all())


@app.get("/api/events")
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
        raise HTTPException(400, "quant enthaelt unerlaubte Zeichen (z.B. 'Q4_K_M')")
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
            "hint": "Fortschritt: GET /api/jobs/%s" % job_id}


@app.get("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        return [{k: v for k, v in j.items() if k != "log"} for j in JOBS.values()]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/")
def root():
    return JSONResponse({
        "service": "llm-box registry",
        "read": ["/api/health", "/api/models", "/api/models/{id}", "/api/gpus",
                 "/api/state", "/api/pi-models.json", "/api/events"],
        "write": ["PATCH /api/models/{id}", "POST /api/models",
                  "POST /api/models/{id}/load", "POST /api/unload",
                  "DELETE /api/models/{id}"],
        "mcp": "/mcp",
        "docs": "/docs",
        "hint": "writing needs the X-LLM-Token header",
    })


# Deliberately mounted at "/" (the MCP app brings its own /mcp path): mounting on
# "/mcp" would redirect POSTs to /mcp/ with a 307, which some MCP clients cannot
# follow. All /api routes are registered before this one and therefore still win.
app.mount("/", mcp.streamable_http_app())


if __name__ == "__main__":
    import uvicorn
    llmreg.api_token(create=True)                     # create it on first start
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
