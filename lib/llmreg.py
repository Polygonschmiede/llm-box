#!/usr/bin/env python3
"""llmreg - the shared library behind the model registry.

The truth about every model lives in config/llama-swap.yaml (runtime) and in
models/<name>/.llm-model.json (provenance). This module reads both, derives the
capabilities from them, and can patch the configuration in place.

Used by:
  * bin/llm         (the bash CLI:  python3 lib/llmreg.py <command>)
  * bin/llm-api.py  (HTTP API + MCP)

Standard library only, so the system Python can load it without a venv.
"""

from __future__ import annotations

import contextlib
import fcntl
import glob
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

#  The two card-detection backends. Beside this file, so importing them needs no
#  path juggling: every entry point already puts lib/ on sys.path to get here.
import gpu_rocm
import gpu_vulkan

#  The fallback is the location of THIS file, not ~/llm: bin/llm calls the
#  library as "python3 lib/llmreg.py <command>", and a checkout in a different
#  directory has to find its own configuration.
LLM_HOME = (os.environ.get("LLM_HOME")
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(LLM_HOME, "config", "llama-swap.yaml")
MODELS = os.path.join(LLM_HOME, "models")
TOKEN_FILE = os.path.join(LLM_HOME, "config", "api-token")
#  Written by 'llm gpu sync'; machine-specific and therefore not in the repo.
HARDWARE_ENV = os.path.join(LLM_HOME, "config", "hardware.env")
COMFY_ENV = os.path.join(LLM_HOME, "config", "comfyui.env")
SWAP_API = os.environ.get("LLM_SWAP_API", "http://127.0.0.1:8080")
def _lan_ip() -> str:
    """First LAN address of this machine, or 127.0.0.1.

    This value travels through derive() -> endpoints.base and
    /api/health.publicApi all the way into the pi integration. Detecting it
    rather than hardcoding it is the difference between "works immediately for
    whoever rebuilds this" and "points at a stranger's network".
    """
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=5).stdout.split()
        return out[0] if out else "127.0.0.1"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "127.0.0.1"


# The address OTHER machines use to reach the OpenAI API (not 127.0.0.1!)
PUBLIC_API = os.environ.get("LLM_PUBLIC_API") or "http://%s:8080/v1" % _lan_ip()
SERVICE = "llama-swap"

META_NAME = ".llm-model.json"
BLOCK_RE = r"# >>> llm:(\S+)\n(.*?)# <<< llm:\1"

#  All card-pinned models go into ONE routing group, not one group per card.
#  The settings would be identical anyway (swap/exclusive false, persistent
#  true), and llama-swap requires that the targets of a 'spillover' selector
#  share a single group - which is exactly the card-0-then-card-1 case.
PINNED_GROUP = "pinned"


# ---------------------------------------------------------------------------
#  Small helpers
# ---------------------------------------------------------------------------
def _http_json(url: str, timeout: float = 3.0, data=None, method=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else None


def _num(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


def flag(cmd: str, name: str):
    """Value of a flag in the cmd line ('-c 8192' -> '8192'). None = absent."""
    m = re.search(r"(?:^|\s)" + re.escape(name) + r"\s+([^\s]+)", cmd)
    return m.group(1) if m else None


def has_flag(cmd: str, name: str) -> bool:
    return re.search(r"(?:^|\s)" + re.escape(name) + r"(?=\s|$)", cmd) is not None


def flag_any(cmd: str, *names):
    """Value of the first flag present. For llama.cpp's short/long pairs
    ('-np' / '--parallel', '-c' / '--ctx-size'), where either may be written."""
    for n in names:
        v = flag(cmd, n)
        if v is not None:
            return v
    return None


def has_flag_any(cmd: str, *names) -> bool:
    return any(has_flag(cmd, n) for n in names)


def slots_of(cmd: str, role: str = "chat") -> tuple[int | None, bool | None]:
    """(slots, kv_unified) as llama-server will really run, not as written.

    llama.cpp treats a missing '-np'/'--parallel' as auto = 4 slots with a
    unified KV cache (tools/server/server.cpp), so reporting the literal flag
    would claim one slot where there are four. whisper-server has neither
    flag, hence the None for role "stt".
    """
    n = _num(flag_any(cmd, "-np", "--parallel"), None)
    auto = n is None or n < 0
    parallel = 4 if auto else n
    unified = auto or has_flag_any(cmd, "-kvu", "--kv-unified")
    if has_flag_any(cmd, "-no-kvu", "--no-kv-unified"):
        unified = False
    if role == "stt":
        return None, None
    return parallel, unified


def set_flag(cmd: str, name: str, value) -> str:
    """Set a flag (replace or append). value=None -> a bare switch.

    The with_value hand-off matters: del_flag() removes the flag AND the token
    after it by default, so setting a valueless switch used to eat whatever
    followed it. On '-np 3 -kvu' plus set_flag('-kvu', None) that produced
    '3 -kvu' - a command line llama-server refuses to start.
    """
    cmd = del_flag(cmd, name, with_value=value is not None)
    return _tidy(cmd + (" %s %s" % (name, value) if value is not None else " " + name))


def del_flag(cmd: str, name: str, with_value: bool = True) -> str:
    pat = r"(?:^|\s)" + re.escape(name) + (r"\s+[^\s]+" if with_value else r"(?=\s|$)")
    return _tidy(re.sub(pat, " ", cmd))


def _tidy(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd).strip()


# ---------------------------------------------------------------------------
#  Reading the configuration
# ---------------------------------------------------------------------------
CONFIG_LOCK = os.path.join(os.path.dirname(CONFIG), ".llama-swap.lock")


@contextlib.contextmanager
def config_lock(timeout: float = 30.0):
    """The shared lock for everyone who touches the configuration.

    Needed since the API can trigger changes too: an 'llm add' (which appends)
    and a PATCH (which rewrites the file) must not overtake each other.
    bin/llm takes the same file - see cfg_lock there.
    """
    os.makedirs(os.path.dirname(CONFIG_LOCK), exist_ok=True)
    fh = open(CONFIG_LOCK, "w", encoding="utf-8")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("configuration is locked right now "
                                       "(another llm command is running)") from None
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


class ConfigMissing(FileNotFoundError):
    """There is no llama-swap.yaml yet. Named so callers can answer usefully.

    config_text() is the entrance to parse_config, find_block, read_selectors,
    sync_groups, sync_tensor_split, gpu_sync, patch_model, remove_model and
    therefore catalog(). On a fresh checkout - the state of every clone before
    'llm init' - a bare FileNotFoundError travelled all the way out as an
    HTTP 500 with a traceback, which tells the caller nothing.
    """


def config_text() -> str:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise ConfigMissing(
            "no configuration at %s - create it with 'llm init'" % CONFIG) from None


def parse_config(text: str | None = None) -> list[dict]:
    """Every marker block as raw data, in file order."""
    text = config_text() if text is None else text
    out = []
    for m in re.finditer(BLOCK_RE, text, re.S):
        name, body = m.group(1), m.group(2)
        if name == "groups":                      # not a model, the GPU groups
            continue
        cm = re.search(r'^\s*cmd:\s*"(.*?)"\s*$', body, re.M | re.S)
        if not cm:
            continue
        over, extra = {}, {}
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("# pi-json:"):
                try:
                    extra.update(json.loads(s.split(":", 1)[1].strip()))
                except ValueError:
                    pass
            elif s.startswith("# pi:"):
                v = re.split(r"\s+#", s[5:].strip())[0].strip()
                k, sep, val = v.partition("=")
                over[k.strip()] = val.strip() if sep else True
        ttl = re.search(r"^\s*ttl:\s*(\d+)", body, re.M)
        env = re.findall(r'^\s*-\s*"([^"]+)"', body, re.M)
        out.append({
            "name": name,
            "cmd": cm.group(1),
            "ttl": int(ttl.group(1)) if ttl else None,
            "env": env,
            "pi_over": over,
            "pi_json": extra,
            "span": m.span(),
            "body": body,
        })
    return out


def find_block(name: str, text: str | None = None):
    text = config_text() if text is None else text
    m = re.search(r"# >>> llm:%s\n(.*?)# <<< llm:%s" % (re.escape(name), re.escape(name)),
                  text, re.S)
    return m


# ---------------------------------------------------------------------------
#  GGUF header data (for a real VRAM estimate instead of a rule of thumb)
# ---------------------------------------------------------------------------
_GGUF_CACHE: dict[str, dict] = {}

# Value types as defined by the GGUF specification
_GT = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_GS = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def gguf_meta(path: str) -> dict:
    """The interesting header fields of a GGUF file (arch, layers, KV heads).

    Reads the metadata header only, never the weights. Falls back to an empty
    dict on anything unexpected - callers have to cope without these values.
    """
    if path in _GGUF_CACHE:
        return _GGUF_CACHE[path]
    out: dict = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"GGUF":
                raise ValueError("not a GGUF file")
            struct.unpack("<I", fh.read(4))          # version
            struct.unpack("<Q", fh.read(8))          # tensor count
            n_kv = struct.unpack("<Q", fh.read(8))[0]

            def rd_str():
                n = struct.unpack("<Q", fh.read(8))[0]
                return fh.read(n).decode("utf-8", "replace")

            def rd_val(t):
                if t == 8:
                    return rd_str()
                if t == 9:                            # Array
                    et = struct.unpack("<I", fh.read(4))[0]
                    n = struct.unpack("<Q", fh.read(8))[0]
                    if et == 8:
                        # Strings have no fixed length, so there is no way around
                        # reading all of them (or the read position slips).
                        vals = [rd_str() for _ in range(n)]
                        return vals if n <= 64 else n
                    if et not in _GT:
                        raise ValueError("array type %s" % et)
                    if n > 4096:                      # skip huge numeric arrays
                        fh.seek(_GS[et] * n, os.SEEK_CUR)
                        return n
                    return list(struct.unpack("<%d%s" % (n, _GT[et]), fh.read(_GS[et] * n)))
                if t not in _GT:
                    raise ValueError("type %s" % t)
                return struct.unpack("<" + _GT[t], fh.read(_GS[t]))[0]

            for _ in range(min(n_kv, 2000)):
                key = rd_str()
                typ = struct.unpack("<I", fh.read(4))[0]
                val = rd_val(typ)
                if key.startswith("tokenizer.") and key != "tokenizer.chat_template":
                    continue
                out[key] = val
    except Exception:      # noqa: BLE001 - a malformed header is "no metadata"
        out = {}
    _GGUF_CACHE[path] = out
    return out


def _arch_get(meta: dict, suffix: str, default=None):
    arch = meta.get("general.architecture", "")
    return meta.get("%s.%s" % (arch, suffix), default)


#  Weights plus KV cache is not the whole story: llama-server also allocates
#  compute buffers, the CUDA/HIP graph and (with -np) one batch per slot. 8 % on
#  top has matched the measurements on this hardware - 32.0 GB estimated against
#  32.17 GB observed for a 27B at 131k context with four slots.
VRAM_HEADROOM = 1.08


def vram_needed(weights: int | None, kv: int | None) -> int | None:
    """What to reserve for a model. None when the weights are unknown."""
    if not weights:
        return None
    return int((weights + (kv or 0)) * VRAM_HEADROOM)


def reasoning_efforts(meta: dict | None) -> dict | None:
    """Which reasoning_effort values the chat template accepts, and its default.

    llama.cpp reports `supports_reasoning_effort: true` and nothing about the
    allowed set, so a client offering the usual OpenAI low/medium/high picker
    gets an HTTP 500 from Jinja on two of the three. Qwen3.8 for instance takes
    only xhigh, medium and low - 'high' raises. The template is in the GGUF
    header, so the set can be read instead of guessed.

    Returns None when the template does not gate the value, which is most of
    them - then anything goes and there is nothing to report.
    """
    tmpl = (meta or {}).get("tokenizer.chat_template") or ""
    if "reasoning_effort" not in tmpl:
        return None
    m = re.search(r"reasoning_effort\s+not\s+in\s*\(([^)]*)\)", tmpl)
    if not m:
        return None
    values = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    if not values:
        return None
    d = re.search(r"reasoning_effort\s*\|\s*default\(\s*['\"]([^'\"]+)", tmpl)
    return {"values": values, "default": d.group(1) if d else None}


def kv_cache_bytes(model_path: str, ctx: int, kv_quant: str | None,
                   parallel: int = 1, meta: dict | None = None) -> int | None:
    """Size of the KV cache in bytes, from the real GGUF header.

    NOTE on `parallel`: the attention KV cache does NOT scale with the slot
    count. `-c` is the total; llama.cpp either shares it across sequences
    (`-kvu`, n_ctx_seq = n_ctx) or divides it (n_ctx_seq = n_ctx / n_seq_max) —
    either way it allocates n_ctx cells. See llama-context.cpp:291-301.
    Only the recurrent/SSM state is held once per sequence and scales.

    Covers the three layouts that occur in practice:
      * classic: every layer holds KV over the full context
      * hybrid (Qwen3.x, 'full_attention_interval'): only every Nth layer has a
        KV cache, the rest are SSM layers with constant state
      * sliding window (Gemma 4): SWA layers only store the window and
        have their own key/value lengths
    """
    #  meta is injectable so the three layout branches below can be checked
    #  against synthetic headers - the arithmetic decides whether a load OOMs,
    #  and a real GGUF fixture would mean committing gigabytes.
    meta = gguf_meta(model_path) if meta is None else meta
    if not meta or not ctx:
        return None
    layers = _arch_get(meta, "block_count")
    heads_kv = _arch_get(meta, "attention.head_count_kv")
    if not layers or heads_kv is None:
        return None
    k_len = _arch_get(meta, "attention.key_length")
    v_len = _arch_get(meta, "attention.value_length")
    if not k_len:
        emb = _arch_get(meta, "embedding_length")
        heads = _arch_get(meta, "attention.head_count")
        k_len = int(emb / heads) if emb and heads else 128
    v_len = v_len or k_len
    k_swa = _arch_get(meta, "attention.key_length_swa") or k_len
    v_swa = _arch_get(meta, "attention.value_length_swa") or v_len
    window = _arch_get(meta, "attention.sliding_window")
    swa_pattern = _arch_get(meta, "attention.sliding_window_pattern")
    interval = _arch_get(meta, "full_attention_interval")

    per = {"q8_0": 1.0625, "q4_0": 0.5625, "q4_1": 0.625, "q5_0": 0.6875,
           "q5_1": 0.75, "f16": 2.0, "bf16": 2.0, "f32": 4.0}
    b = per.get((kv_quant or "f16").lower(), 2.0)

    elems = 0
    for i in range(int(layers)):
        h = heads_kv[i] if isinstance(heads_kv, list) and i < len(heads_kv) else (
            heads_kv if not isinstance(heads_kv, list) else 0)
        if interval and (i + 1) % int(interval) != 0:
            continue                                # SSM layer, no KV cache
        if not h:
            continue
        is_swa = bool(swa_pattern[i]) if isinstance(swa_pattern, list) and i < len(swa_pattern) \
            else False
        if is_swa and window:
            elems += h * (k_swa + v_swa) * min(ctx, int(window))
        else:
            elems += h * (k_len + v_len) * ctx
    total = elems * b

    # State of the SSM layers (constant, not context-dependent) — but one state
    # PER SEQUENCE, so this is the only part that scales with the slot count.
    ssm = 0
    ssm_inner = _arch_get(meta, "ssm.inner_size")
    if interval and ssm_inner:
        ssm_layers = int(layers) - int(layers) // int(interval)
        conv = (_arch_get(meta, "ssm.conv_kernel") or 0) * ssm_inner
        state = (_arch_get(meta, "ssm.state_size") or 0) * ssm_inner
        ssm = ssm_layers * (conv + state) * 4                     # f32

    return int(total + ssm * max(1, parallel))


# ---------------------------------------------------------------------------
#  Provenance (sidecar models/<name>/.llm-model.json)
# ---------------------------------------------------------------------------
def meta_path(model_dir: str) -> str:
    return os.path.join(model_dir, META_NAME)


def read_meta(model_dir: str) -> dict | None:
    p = meta_path(model_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return None


def write_meta(model_dir: str, data: dict) -> str:
    os.makedirs(model_dir, exist_ok=True)
    p = meta_path(model_dir)
    old = read_meta(model_dir) or {}
    old.update({k: v for k, v in data.items() if v is not None})
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(old, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def hf_commit(model_dir: str) -> str | None:
    """The commit hash 'hf download' left behind in its cache."""
    dl = os.path.join(model_dir, ".cache", "huggingface", "download")
    for root, _dirs, files in os.walk(dl):
        for f in files:
            if f.endswith(".metadata"):
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        sha = fh.readline().strip()
                    if re.fullmatch(r"[0-9a-f]{40}", sha):
                        return sha
                except OSError:
                    pass
    trees = os.path.join(model_dir, ".cache", "huggingface", "trees")
    if os.path.isdir(trees):
        for f in sorted(os.listdir(trees)):
            if f.endswith(".json") and re.fullmatch(r"[0-9a-f]{40}\.json", f):
                return f[:-5]
    return None


def hf_verify(repo: str, sha: str) -> str | None:
    """Does this commit really belong to this repo? Returns the exact repo id.

    Hugging Face accepts the id case-insensitively but answers with the
    canonical spelling, which is the one worth storing.
    """
    try:
        d = _http_json("https://huggingface.co/api/models/%s/revision/%s" % (repo, sha),
                       timeout=15)
        return (d or {}).get("id") or repo
    except Exception:      # noqa: BLE001 - no Hugging Face, no revision
        return None


def hf_search(query: str, limit: int = 20, gguf_only: bool = True) -> list[str]:
    try:
        data = _http_json(
            "https://huggingface.co/api/models?search=%s&limit=%d%s"
            % (urllib.parse.quote(query), limit, "&filter=gguf" if gguf_only else ""),
            timeout=15)
        return [d.get("id", "") for d in (data or []) if d.get("id")]
    except Exception:      # noqa: BLE001 - a failed search is an empty search
        return []


_TREE_CACHE: dict[str, dict] = {}


def file_digest(path: str) -> str | None:
    """sha256 from the HF cache metadata (never recomputed - 30 GB takes a while)."""
    d = os.path.dirname(path)
    while d and d.startswith(MODELS):
        trees = os.path.join(d, ".cache", "huggingface", "trees")
        if os.path.isdir(trees):
            if d not in _TREE_CACHE:
                merged: dict = {}
                for f in sorted(os.listdir(trees)):
                    try:
                        with open(os.path.join(trees, f), encoding="utf-8") as fh:
                            merged.update((json.load(fh).get("files") or {}))
                    except (OSError, ValueError):
                        continue
                _TREE_CACHE[d] = merged
            info = _TREE_CACHE[d].get(os.path.relpath(path, d))
            return (info.get("lfs_sha256") or info.get("blob_id")) if info else None
        d = os.path.dirname(d)
    return None


# ---------------------------------------------------------------------------
#  Runtime state (llama-swap) and the GPUs
# ---------------------------------------------------------------------------
def live() -> dict:
    """What llama-swap currently reports. Service down -> empty but valid answer."""
    out = {"up": False, "states": {}, "running": []}
    try:
        data = _http_json(SWAP_API + "/v1/models")
        out["up"] = True
        for m in (data or {}).get("data", []):
            out["states"][m.get("id")] = (m.get("status") or {}).get("value", "unknown")
    except Exception:      # noqa: BLE001 - llama-swap down is a state, not an error
        return out
    try:
        run = _http_json(SWAP_API + "/running") or {}
        out["running"] = run.get("running", [])
    except Exception:      # noqa: BLE001 - as above: report what we know
        pass
    return out


#  ---------------------------------------------------------------------------
#  TWO CARD NUMBERINGS - the easiest thing to get wrong in this file:
#
#    absolute = how rocm-smi counts. This is the value HIP_VISIBLE_DEVICES wants.
#    logical  = the position WITHIN HIP_VISIBLE_DEVICES. This is the N in
#               '--device ROCmN', in '-mg N' and in the groups 'gpuN'.
#
#  Measured: with HIP_VISIBLE_DEVICES=1 the second card shows up as ROCm0.
#  On a machine without an iGPU both numbers are the same - as soon as a card
#  sits in between (or one is hidden), they are not. Outwards (CLI, API, pi) we
#  always show the LOGICAL number; only code that writes HIP_VISIBLE_DEVICES
#  translates first, with to_smi().
#  ---------------------------------------------------------------------------

#  ---------------------------------------------------------------------------
#  TWO BACKENDS behind one interface (see lib/gpu_rocm.py for the contract).
#
#  ROCm and Vulkan differ in four things and in nothing else that matters here:
#  how cards are enumerated, what '--device <prefix>N' is called, which
#  environment variable hides a card from the runtime, and what cmake needs. So
#  each backend is a module answering cards()/gfx_targets()/compiler(), and the
#  part this project keeps getting wrong - absolute versus logical numbering -
#  stays here, written once, shared.
#
#  The choice is a machine fact like the card count, so it lives in
#  config/hardware.env next to the visible-devices mask, written by
#  'llm gpu sync'. LLM_BACKEND in the environment wins over the file, and the
#  systemd units pull the file in with EnvironmentFile=- so the services agree
#  with the CLI.
#  ---------------------------------------------------------------------------
BACKENDS = ("rocm", "vulkan")


def _env_file_get(path: str, key: str) -> str | None:
    """One KEY=value out of a systemd EnvironmentFile, without importing it."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip() or None
    except OSError:
        #  No file yet (a fresh clone, or 'llm gpu sync' has not run), or it is
        #  unreadable. Both mean "nothing recorded", which is a valid answer here
        #  and lets the caller fall through to detection.
        pass
    return None


def backend_name() -> str:
    """'rocm' or 'vulkan'. Explicit choice first, then the file, then detection.

    Detection prefers ROCm where it is complete: it is the faster path on the
    hardware this project was built for, and every existing installation is
    already on it. Vulkan is the answer for everything else - it needs no
    AMDGPU_TARGETS and no HIP compiler, so it cannot be built for the wrong card.
    """
    want = (os.environ.get("LLM_BACKEND") or "").strip().lower()
    if want not in BACKENDS:
        want = (_env_file_get(HARDWARE_ENV, "LLM_BACKEND") or "").strip().lower()
    if want in BACKENDS:
        return want
    if gpu_rocm.available():
        return "rocm"
    return "vulkan" if gpu_vulkan.available() else "rocm"


def backend():
    """The module for the active backend."""
    return gpu_vulkan if backend_name() == "vulkan" else gpu_rocm


def device_prefix() -> str:
    """'ROCm' or 'Vulkan' - the N in '--device <this>N' is always LOGICAL."""
    return backend().DEVICE_PREFIX


def visible_env() -> str:
    """HIP_VISIBLE_DEVICES or GGML_VK_VISIBLE_DEVICES, whichever applies."""
    return backend().VISIBLE_ENV


#  Both prefixes and both variable names are always ACCEPTED when reading a
#  configuration, whichever backend is active. A config that survives a backend
#  switch is worth more than a strict parser: the alternative is every model
#  silently losing its card pinning the moment someone runs 'llm gpu backend'.
_ALL_DEVICE_PREFIXES = tuple(m.DEVICE_PREFIX for m in (gpu_rocm, gpu_vulkan))
_ALL_VISIBLE_ENVS = tuple(m.VISIBLE_ENV for m in (gpu_rocm, gpu_vulkan))

#  Kept as a module attribute because the tests and docs name it.
ROCM_SMI = gpu_rocm.SMI

#  Fallback filter for a backend that cannot say whether a device is discrete.
#  rocm-smi cannot: the iGPU appears as another "GPU" carrying a CPU name, and
#  filtering by VRAM alone is unreliable - rocm-smi reports 0.5 GB for that
#  device while HIP reports 31 GB of system memory for the SAME one, so on an APU
#  with a large UMA carve-out a threshold flips. The name is the more dependable
#  signal, the threshold only a backstop. Vulkan needs neither: it states the
#  device type.
_CPU_NAME_RE = gpu_rocm.CPU_NAME_RE
_MIN_DGPU_VRAM = _num(os.environ.get("LLM_MIN_VRAM_GB"), 2) * 1024**3

_SMI_CACHE: tuple[str, float, dict[int, dict]] | None = None


def _smi_cards(max_age: float = 1.0) -> dict[int, dict]:
    """Every device the active backend knows about, iGPU included.

    Keys are absolute indices - the numbers the visible-devices mask wants.
    Cached briefly because several callers ask within the same request (gpus,
    gpu_of, check_fit); 1 s is short enough to keep 'llm watch' live. Keyed by
    backend as well, so a switch inside one process is not served the other
    one's answer.
    """
    global _SMI_CACHE
    name = backend_name()
    if _SMI_CACHE and _SMI_CACHE[0] == name and (time.time() - _SMI_CACHE[1]) < max_age:
        return _SMI_CACHE[2]
    cards = backend().cards()
    _SMI_CACHE = (name, time.time(), cards)
    return cards


def dgpu_smi_indices() -> list[int]:
    """Absolute indices of the real compute cards, ascending.

    Three stages: an explicit list (LLM_DGPUS) -> the name -> the VRAM threshold.
    """
    cards = _smi_cards()
    if not cards:
        return []
    forced = os.environ.get("LLM_DGPUS", "").strip()
    if forced:
        want = {int(x) for x in re.findall(r"\d+", forced)}
        return sorted(i for i in cards if i in want)
    #  A backend that knows gets to say so. Vulkan reports the device type, so
    #  the integrated GPU and llvmpipe - which Vulkan offers and nobody wants a
    #  model on - are excluded by fact rather than by brand name.
    if cards and all("discrete" in c for c in cards.values()):
        keep = [i for i, c in cards.items() if c["discrete"]]
        return sorted(keep) if keep else sorted(cards)
    keep = [i for i, c in cards.items()
            if not _CPU_NAME_RE.search(c.get("name") or "")
            and (c.get("vramTotalBytes") or 0) >= _MIN_DGPU_VRAM]
    #  Better to report everything than nothing: if the detection discards ALL of
    #  them, the rule is more likely wrong than the hardware - and "no card" is
    #  the more harmful answer, because then every fit check fails.
    return sorted(keep) if keep else sorted(cards)


def gpus() -> list[dict]:
    """VRAM and temperature per compute card; the iGPU is filtered out.

    'index' is the LOGICAL number (see the block above), 'smiIndex' the absolute one.
    """
    cards = _smi_cards()
    pinned = pinned_models()
    out = []
    for logical, smi in enumerate(dgpu_smi_indices()):
        c = dict(cards[smi], index=logical)
        tot = c.get("vramTotalBytes") or 0
        c["vramFreeBytes"] = tot - (c.get("vramUsedBytes") or 0)
        c["pinnedModels"] = pinned.get(logical, [])
        out.append(c)
    return out


def gpu_count() -> int:
    return len(dgpu_smi_indices())


def to_smi(logical) -> int | None:
    """Logical card number -> absolute (what the visible-devices mask wants)."""
    idx = _num(logical)
    dg = dgpu_smi_indices()
    return dg[idx] if isinstance(idx, int) and 0 <= idx < len(dg) else None


def to_logical(smi) -> int | None:
    """Absolute card number -> logical. None = not a compute card."""
    idx = _num(smi)
    dg = dgpu_smi_indices()
    return dg.index(idx) if idx in dg else None


def gfx_targets() -> str:
    """ISA targets of the compute cards, for the build. '' when not applicable.

    Under ROCm this is AMDGPU_TARGETS, e.g. 'gfx1201' or 'gfx1100;gfx1201', and
    discrete cards only - building for the iGPU's target is wasted time and fails
    outright on some ROCm versions. Under Vulkan it is empty: SPIR-V is compiled
    once and runs on every device.
    """
    return backend().gfx_targets(_smi_cards(), dgpu_smi_indices())


def hip_compiler() -> str | None:
    """Path to the backend's compiler, where it needs a particular one.

    ROCm needs CMAKE_HIP_COMPILER; Vulkan uses the ordinary C++ compiler and
    answers None.
    """
    return backend().compiler()


def tensor_split() -> str | None:
    """An even split for -ts, or None with exactly one card.

    Without -ts llama.cpp distributes by FREE VRAM at load time
    (llama.cpp/src/llama-model.cpp, 'default split, by free memory'), which is
    not reproducible when one card already holds something. So we state the
    split explicitly rather than leaving it out.
    """
    n = gpu_count()
    return ",".join(["1"] * n) if n > 1 else None


def hw() -> dict:
    """Everything that depends on the hardware, in one answer."""
    cards = gpus()
    warnings = []
    name = backend_name()
    if not cards:
        tool = "rocm-smi" if name == "rocm" else "vulkaninfo"
        warnings.append("no compute card detected - is %s working? is the user "
                        "in the render and video groups?" % tool)
    if len({c.get("vramTotalBytes") for c in cards}) > 1:
        warnings.append("cards of different size: the even -ts split does not fit "
                        "that case, see docs/FLAGS.md.")
    #  hipVisibleDevices keeps its name in the payload even under Vulkan, where
    #  the variable is called GGML_VK_VISIBLE_DEVICES. Renaming a documented API
    #  field to say the same thing differently would break the pi extension and
    #  the control page for no gain; visibleEnv says which name it is written
    #  under, and that is the part a caller actually needs.
    return {
        "backend": name,
        "visibleEnv": visible_env(),
        "dgpus": cards,
        "hipVisibleDevices": ",".join(str(i) for i in dgpu_smi_indices()),
        "gfxTargets": gfx_targets(),
        "hipCompiler": hip_compiler(),
        "tensorSplit": tensor_split(),
        "warnings": warnings,
    }


def pinned_models() -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for e in parse_config():
        dev = gpu_of(e)
        if dev.get("mode") == "single":
            out.setdefault(dev["device"], []).append(e["name"])
    return out


def gpu_of(entry: dict) -> dict:
    """Which card does this model run on? Whisper steers that through env."""
    dev = flag(entry["cmd"], "--device")
    for prefix in _ALL_DEVICE_PREFIXES:
        if dev and dev.startswith(prefix):
            n = _num(dev[len(prefix):])
            return {"mode": "single", "device": n, "group": PINNED_GROUP, "via": "flag"}
    for e in entry.get("env") or []:
        m = re.match(r"(?:%s)=([\d,]+)" % "|".join(_ALL_VISIBLE_ENVS), e)
        if m:
            #  The env holds the ABSOLUTE number (that is how HIP counts);
            #  outwards we report the logical one. Several indices = no pinning.
            idxs = [int(x) for x in m.group(1).split(",") if x != ""]
            if len(idxs) != 1:
                return {"mode": "both", "device": None, "group": None, "via": "env"}
            n = to_logical(idxs[0])
            if n is None:                 # points at something that is not a compute card
                return {"mode": "both", "device": None, "group": None, "via": "env"}
            #  sync_groups() picks up env-pinned models too, so report the group.
            return {"mode": "single", "device": n, "group": PINNED_GROUP, "via": "env"}
    return {"mode": "both", "device": None, "group": None, "via": "macro"}


# ---------------------------------------------------------------------------
#  Derivation: what can this model do?
# ---------------------------------------------------------------------------
def role_of(cmd: str) -> str:
    if "whisper-server" in cmd:
        return "stt"
    if "--reranking" in cmd or "server-rerank" in cmd:
        return "rerank"
    if "--embedding" in cmd or "server-embed" in cmd:
        return "embed"
    return "chat"


def derive(entry: dict, want_gguf: bool = True) -> dict:
    """A complete catalog entry, without any live state."""
    name, cmd, over = entry["name"], entry["cmd"], entry["pi_over"]
    role = role_of(cmd)
    macro_m = re.search(r"\$\{(server[^}]*)\}", cmd)
    macro = macro_m.group(1) if macro_m else ("whisper" if role == "stt" else None)

    model_file = flag(cmd, "-m")
    mmproj = flag(cmd, "--mmproj")
    draft = flag(cmd, "--model-draft")
    ctx = _num(over.get("contextWindow"), _num(flag(cmd, "-c")))
    kv_quant = flag(cmd, "-ctk")

    # Careful: for MTP/ngram the --spec-type lives in the MACRO (${server-mtp}),
    # not in the cmd line - so the macro name is part of the detection.
    spec = "none"
    if macro == "server-mtp" or flag(cmd, "--spec-type") == "draft-mtp" or draft:
        spec = "mtp"
    elif macro == "server-ngram" or (flag(cmd, "--spec-type") or "").startswith("ngram"):
        spec = "ngram"

    if "reasoning" in over:
        reasoning = str(over["reasoning"]).lower() in ("1", "true", "ja", "yes")
    else:
        reasoning = role == "chat" and not (
            flag(cmd, "-rea") == "off" or flag(cmd, "--reasoning-budget") == "0"
            or "coder" in name)

    if "input" in over:
        inputs = [x.strip() for x in str(over["input"]).split(",") if x.strip()]
    else:
        inputs = ["text", "image"] if mmproj else ["text"]

    sampling = {}
    for f, key in (("--temp", "temperature"), ("--top-p", "top_p"),
                   ("--top-k", "top_k"), ("--min-p", "min_p")):
        v = flag(cmd, f)
        if v is not None:
            sampling[key] = _num(v)

    files, weights, issues = {}, 0, []
    for key, path in (("model", model_file), ("mmproj", mmproj), ("draft", draft)):
        if not path:
            continue
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else None
        files[key] = {"path": path, "sizeBytes": size, "sha256": file_digest(path),
                      "exists": exists}
        weights += size or 0
        if not exists:
            # Deleted by hand, or the disk is not mounted: llama-swap would fail
            # to start. The catalog has to say so, or it is not being honest.
            issues.append("%s file missing: %s" % (key, path))

    model_dir = os.path.dirname(model_file) if model_file else None
    if model_dir and os.path.basename(os.path.dirname(model_dir)) == "models":
        pass                                        # model file sits directly in the folder
    elif model_dir:
        #  Walk up to the directory sitting directly under MODELS. The root
        #  check is not cosmetic: for a -m path OUTSIDE models/ - another disk,
        #  a hand-edited config - this used to spin forever, because
        #  os.path.dirname("/") == "/". A single such entry hung every caller of
        #  catalog(), i.e. GET /api/models never answered.
        while model_dir != MODELS and os.path.dirname(model_dir) != MODELS:
            parent = os.path.dirname(model_dir)
            if parent == model_dir:                 # filesystem root, not under MODELS
                model_dir = None
                break
            model_dir = parent
    src = read_meta(model_dir) if model_dir else None
    if src and src.get("repo"):
        src = dict(src, url="https://huggingface.co/" + src["repo"])
    else:
        issues.append("provenance unknown (on the server: llm meta backfill)")

    gguf = gguf_meta(model_file) \
        if (want_gguf and model_file and os.path.exists(model_file)) else {}
    parallel, kv_unified = slots_of(cmd, role)
    #  What the template accepts, and the floor this entry sets on the command
    #  line. A client that sends nothing gets the floor; one that sends a value
    #  overrides it (server-common.cpp merges CLI kwargs first, request second).
    efforts = reasoning_efforts(gguf)
    effort_default = flag(cmd, "--reasoning-effort")
    preserve = not has_flag_any(cmd, "--no-reasoning-preserve", "-no-rp")
    kv_bytes = kv_cache_bytes(model_file, ctx, kv_quant, parallel or 1) \
        if (model_file and ctx) else None

    endpoint = {"chat": "/chat/completions", "embed": "/embeddings",
                "rerank": "/rerank", "stt": "/audio/transcriptions"}[role]

    out = {
        "id": name,
        "role": role,
        "ttl": entry["ttl"],
        "runtime": {
            "macro": macro,
            "contextWindow": ctx,
            "gpu": gpu_of(entry),
            "specDecoding": spec,
            "draftModel": draft,
            "kvCacheQuant": kv_quant,
            "parallel": parallel,
            "kvUnified": kv_unified,
            "reasoningEffort": {
                #  None = the template does not gate the value, so anything goes.
                "accepts": (efforts or {}).get("values"),
                "templateDefault": (efforts or {}).get("default"),
                "serverDefault": effort_default,
                "preserveThinking": preserve,
            } if role == "chat" else None,
            "mmproj": mmproj,
            "cmd": cmd,
        },
        "capabilities": {
            "chat": role == "chat",
            # --jinja, and therefore tool calling, lives in the chat macros
            "tools": role == "chat" and (has_flag(cmd, "--jinja")
                                         or macro in ("server", "server-mtp", "server-ngram")),
            "vision": "image" in inputs,
            "reasoning": reasoning,
            "embeddings": role == "embed",
            "rerank": role == "rerank",
            "transcription": role == "stt",
        },
        "sampling": sampling,
        "files": files,
        "vram": {
            "weightsBytes": weights or None,
            "kvCacheBytes": kv_bytes,
            # Headroom for compute context and fragmentation: measured ~8 percent.
            # Without a KV figure (Whisper) only the weights plus headroom remain.
            "estimatedBytes": vram_needed(weights, kv_bytes),
        },
        "source": src,
        "architecture": {
            "name": gguf.get("general.name"),
            "arch": gguf.get("general.architecture"),
            "layers": _arch_get(gguf, "block_count"),
            "nativeContext": _arch_get(gguf, "context_length"),
            "parameters": gguf.get("general.parameter_count"),
        } if gguf else None,
        "endpoints": {"base": PUBLIC_API, "path": endpoint},
        "issues": issues,
    }
    return out


# ---------------------------------------------------------------------------
#  The pi format
# ---------------------------------------------------------------------------
def pi_entry(model: dict) -> dict | None:
    """One entry for pi's models.json. None = not a chat model."""
    if model["role"] != "chat":
        return None
    over = model.get("_pi_over", {})
    # '# pi: skip' (no value) and '# pi: skip=true' (how the API writes it)
    skip = over.get("skip")
    #  "ja" is accepted for backwards compatibility with configurations written
    #  before this file was translated; documented in docs/PI.md.
    if skip is True or str(skip).strip().lower() in ("1", "true", "yes", "ja"):
        return None
    ctx = model["runtime"]["contextWindow"] or 8192
    entry = {"id": model["id"]}
    if over.get("name"):
        entry["name"] = over["name"]
    entry["reasoning"] = model["capabilities"]["reasoning"]
    entry["input"] = ["text", "image"] if model["capabilities"]["vision"] else ["text"]
    entry["contextWindow"] = ctx
    entry["maxTokens"] = _num(over.get("maxTokens"), min(ctx, 32768))
    if model["sampling"]:
        entry["samplingParams"] = model["sampling"]
    tf = over.get("thinkingFormat")
    if tf is None and entry["reasoning"] and model["id"].startswith("qwen"):
        tf = "qwen-chat-template"
    if tf:
        entry.setdefault("compat", {})["thinkingFormat"] = tf
    #  reasoning_effort is a PER REQUEST decision, not a per server one:
    #  llama-server accepts the OpenAI field and passes it to the chat template
    #  (llama.cpp/tools/server/server-common.cpp). It only has an effect on a
    #  thinking model, so we report the capability per model instead of denying
    #  it provider-wide. This sits before the '# pi-json:' block so an explicit
    #  override still wins.
    entry.setdefault("compat", {})["supportsReasoningEffort"] = entry["reasoning"]
    #  And WHICH values, when the template gates them. Without this a client
    #  offering the usual low/medium/high gets a Jinja exception on 'high'.
    re_info = (model["runtime"].get("reasoningEffort") or {})
    if entry["reasoning"] and re_info.get("accepts"):
        entry["compat"]["reasoningEfforts"] = re_info["accepts"]
        if re_info.get("serverDefault") or re_info.get("templateDefault"):
            entry["compat"]["reasoningEffortDefault"] = (
                re_info.get("serverDefault") or re_info.get("templateDefault"))
    for k, v in (model.get("_pi_json") or {}).items():
        if k == "compat":
            entry.setdefault("compat", {}).update(v)
        else:
            entry[k] = v
    return entry


def pi_models_json(models: list[dict] | None = None) -> dict:
    #  Finished catalog entries already carry their pi block (None = do not
    #  report this model to pi, e.g. because of '# pi: skip'). Only raw entries
    #  still need converting - otherwise the overrides catalog() already
    #  processed would be lost.
    models = catalog() if models is None else models
    entries = [e for e in ((m["pi"] if "pi" in m else pi_entry(m)) for m in models) if e]
    return {"providers": {"llm-box": {
        "baseUrl": PUBLIC_API,
        "api": "openai-completions",
        #  The real key when the endpoint is authenticated, otherwise the
        #  placeholder - pi wants to see something, and llama-swap does not
        #  check the value when no apiKeys block is present. A client that
        #  refreshes from the registry therefore picks up a rotation by itself.
        "apiKey": api_key() or "sk-local",
        #  llama-server can handle the developer role: it maps it to 'system'
        #  internally (llama.cpp/common/chat.cpp, map_developer_role_to_system),
        #  so it holds for every model and belongs here.
        #  'supportsReasoningEffort' depends on the model and therefore lives in
        #  the individual entry (see pi_entry).
        "compat": {"supportsDeveloperRole": True},
        "models": entries,
    }}}


# ---------------------------------------------------------------------------
#  The catalog
# ---------------------------------------------------------------------------
def recheck_files(m: dict) -> dict:
    """Re-check that the files exist.

    The expensive part of the catalog (GGUF headers, checksums) may be cached;
    this may not: a file can disappear without the configuration changing, and
    then llama-swap would fail to start.
    """
    issues = [i for i in m.get("issues", []) if "file missing:" not in i]
    weights = 0
    for key, f in (m.get("files") or {}).items():
        exists = os.path.exists(f["path"])
        f["exists"] = exists
        f["sizeBytes"] = os.path.getsize(f["path"]) if exists else None
        if not exists:
            issues.append("%s file missing: %s" % (key, f["path"]))
        weights += f["sizeBytes"] or 0
    m["issues"] = issues
    vram = m.get("vram") or {}
    vram["weightsBytes"] = weights or None
    kv = vram.get("kvCacheBytes")
    vram["estimatedBytes"] = vram_needed(weights, kv)
    m["vram"] = vram
    return m


def selector_catalog(models: list[dict]) -> list[dict]:
    """Catalog entries for the roles, derived from their target models.

    A role must not promise more than its weakest target, so capabilities are
    INTERSECTED and the context window is the MINIMUM. Otherwise a client would
    happily send 131k tokens to a role whose second target holds 8k.
    """
    by_id = {m["id"]: m for m in models}
    out = []
    for name, sel in sorted(read_selectors().items()):
        targets = [by_id[t] for t in sel["targets"] if t in by_id]
        if not targets:
            continue
        caps: dict[str, bool] = {}
        for key in targets[0]["capabilities"]:
            caps[key] = all(t["capabilities"].get(key) for t in targets)
        ctxs = [t["runtime"]["contextWindow"] for t in targets
                if t["runtime"]["contextWindow"]]
        roles = {t["role"] for t in targets}
        #  A role over mixed kinds of model is not something a client can use.
        role = targets[0]["role"] if len(roles) == 1 else "mixed"
        ready = [t["id"] for t in targets if t["state"] in ("ready", "loading")]
        entry = {
            "id": name,
            "kind": "role",
            "role": role,
            "ttl": None,
            "runtime": {
                "selector": {
                    "strategy": sel["strategy"],
                    "targets": [t["id"] for t in targets],
                    "spillover": (sel.get("settings") or {}).get("spillover"),
                },
                "contextWindow": min(ctxs) if ctxs else None,
                "gpu": {"mode": "role", "device": None,
                        "group": PINNED_GROUP, "via": "selector"},
                "cmd": None,
            },
            "capabilities": caps,
            "sampling": {},
            "vram": None,
            "endpoints": targets[0]["endpoints"],
            "description": sel.get("description") or "",
            "issues": [],
            "state": "ready" if ready else "unloaded",
            "activeTargets": ready,
        }
        if sel.get("name"):
            entry["name"] = sel["name"]
        entry["pi"] = pi_entry(dict(entry, _pi_over={}, _pi_json=None)) \
            if role == "chat" else None
        out.append(entry)
    return out


def catalog(with_live: bool = True, want_gguf: bool = True) -> list[dict]:
    entries = parse_config()
    state = live() if with_live else {"up": False, "states": {}, "running": []}
    running = {r.get("model"): r for r in state["running"]}
    out = []
    for e in entries:
        m = derive(e, want_gguf=want_gguf)
        m["_pi_over"] = e["pi_over"]
        m["_pi_json"] = e["pi_json"]
        m["state"] = state["states"].get(e["name"], "unknown" if not state["up"] else "unloaded")
        r = running.get(e["name"])
        if r:
            m["state"] = r.get("state", "ready")
            m["runtime"]["proxy"] = r.get("proxy")
        m["pi"] = pi_entry(m)
        m.pop("_pi_over", None)
        m.pop("_pi_json", None)
        m["kind"] = "model"
        out.append(m)
    #  Roles come last and are derived from the models above.
    return out + selector_catalog(out)


def get_model(name: str, **kw) -> dict | None:
    for m in catalog(**kw):
        if m["id"] == name:
            return m
    return None


# ---------------------------------------------------------------------------
#  Writing the configuration
# ---------------------------------------------------------------------------
def put_block(text: str, mark: str, body: str, head: str = "") -> str:
    """Replace the '# >>> llm:<mark>' block, or CREATE it before 'models:'.

    Creating rather than silently doing nothing matters: sync_groups() used to
    return the text unchanged when its marker was missing, and all four callers
    reported success - while every card-pinned model quietly stayed in
    llama-swap's default group, which swaps and is exclusive.

    New blocks go BEFORE 'models:'. Appending at the end of the file would put
    them after it, and the next 'llm add' - which appends its two-space model
    block at the bottom - would hang that model under the wrong key.
    """
    block = "# >>> llm:%s\n%s# <<< llm:%s" % (mark, body, mark)
    pat = r"# >>> llm:%s\n.*?# <<< llm:%s" % (re.escape(mark), re.escape(mark))
    if re.search(pat, text, re.S):
        return re.sub(pat, lambda _m: block, text, flags=re.S)
    m = re.search(r"^models:", text, re.M)
    if not m:
        raise ValueError("the configuration has no 'models:' section - "
                         "is this a llama-swap config? (llm init)")
    #  Walk back over the comment header that belongs to 'models:' so it stays
    #  attached to it instead of ending up above the new block. Stop at a marker
    #  line: those start with '#' too, and walking past one would insert the new
    #  block INSIDE the previous one, splitting it from its closing marker.
    lines = text[:m.start()].split("\n")
    while lines and (not lines[-1].strip()
                     or (lines[-1].startswith("#")
                         and not lines[-1].startswith("# <<< llm:")
                         and not lines[-1].startswith("# >>> llm:"))):
        lines.pop()
    cut = len("\n".join(lines))
    if cut:
        cut += 1
    sep = "\n" if cut and not text[:cut].endswith("\n\n") else ""
    return text[:cut] + sep + head + block + "\n\n" + text[cut:]


def _write_config(text: str) -> None:
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    shutil.copymode(CONFIG, tmp)
    #  An apiKeys entry makes this file a secret, so it stops being world
    #  readable. 'llm key new' wrote the key to config/api-key with mode 600 and
    #  then a copy of it into here, which 'llm init' had created 644 - the
    #  careful mode on the small file was undone by the large one beside it.
    #  Narrowing costs nothing: llama-swap runs as the same user.
    #  Done here rather than in sync_api_key because this is the one place every
    #  config write passes through, so the mode cannot drift back afterwards.
    if re.search(r"^\s*apiKeys:", text, re.M):
        os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def sync_groups(text: str | None = None) -> str:
    """Regenerate the routing group from whatever pins a model to a card.

    Uses gpu_of(), so BOTH ways of pinning count: '--device ROCmN' and
    'env: HIP_VISIBLE_DEVICES=N'. Reading only the flag used to leave the
    env-pinned whisper entries out, which put them in llama-swap's default
    group - swapping and exclusive - so every transcription unloaded the
    pinned models on both cards.

    The group is written with:
      swap: false       members do not evict each other
      exclusive: false  and do not evict other groups
      persistent: true  and no other group may evict THEM, so a model that
                        spans all cards no longer throws the service models out
    """
    text = config_text() if text is None else text
    members: list[tuple[str, int]] = []
    for entry in parse_config(text):
        g = gpu_of(entry)
        if g["mode"] == "single" and g["device"] is not None:
            members.append((entry["name"], int(g["device"])))
    members.sort(key=lambda x: (x[1], x[0]))
    block = ""
    if members:
        block = ("groups:\n  %s:\n    swap: false\n    exclusive: false\n"
                 "    persistent: true\n    members:\n" % PINNED_GROUP)
        width = max(len(n) for n, _ in members) + 2
        for name, card in members:
            block += '      - %-*s # card %d\n' % (width, '"%s"' % name, card)
    head = ("# " + "=" * 76 + "\n"
            "#  CARD GROUPS  —  maintained by 'llm add --gpu N' / 'llm gpu sync'\n"
            "# " + "=" * 76 + "\n"
            "#  Every model pinned to a card goes into ONE group called '%s' -\n"
            "#  whether it was pinned with the --device flag or with the\n"
            "#  visible-devices mask in env: (whisper has no --device flag).\n"
            "#  Both are spelled per backend; 'llm gpu sync' keeps them current.\n"
            "#   swap: false      -> members do not evict each other\n"
            "#   exclusive: false -> and do not evict anything from other groups\n"
            "#   persistent: true -> and NO other group may evict them either\n"
            "#  One group and not one per card, because a 'spillover' role needs\n"
            "#  all of its targets in a single group.  DO NOT edit by hand:\n"
            % PINNED_GROUP)
    return put_block(text, "groups", block, head)


# ---------------------------------------------------------------------------
#  Roles (llama-swap "selectors"): virtual model names -> real models
# ---------------------------------------------------------------------------
#  A client asks for a ROLE and llama-swap picks a target for it. That way
#  clients and their subagents never need to know a file name.
#
#  Deliberately parsed by hand instead of with PyYAML: venv-api does not have
#  it, and the block is machine-generated, so its shape is fixed.
SELECTOR_STRATEGIES = ("warm", "pin", "spillover")
_SEL_MARK = "selectors"


def read_selectors(text: str | None = None) -> dict:
    """The roles from the marker block, as {name: {strategy, targets, ...}}."""
    text = config_text() if text is None else text
    m = re.search(r"# >>> llm:%s\n(.*?)# <<< llm:%s" % (_SEL_MARK, _SEL_MARK),
                  text, re.S)
    if not m:
        return {}
    out: dict[str, dict] = {}
    cur = None
    listing = None                                  # which key the '- ' items go to
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#") or line.strip() == "selectors:":
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 2 and body.endswith(":"):      # a role name
            cur = body[:-1].strip().strip('"')
            out[cur] = {"strategy": "warm", "targets": []}
            listing = None
            continue
        if cur is None:
            continue
        if body.startswith("- "):
            if listing == "targets":
                out[cur]["targets"].append(body[2:].strip().strip('"'))
            continue
        key, _, val = body.partition(":")
        key, val = key.strip(), val.strip().strip('"')
        if not val:                                 # 'targets:' / 'settings:'
            listing = key
            continue
        listing = None
        if key == "spillover":                      # lives under settings:
            out[cur].setdefault("settings", {})["spillover"] = _num(val, 1)
        elif key in ("strategy", "name", "description"):
            out[cur][key] = val
    return out


def render_selectors(sel: dict) -> str:
    """The YAML for the marker block. Empty dict -> no 'selectors:' key at all."""
    if not sel:
        return ""
    out = "selectors:\n"
    for name in sorted(sel):
        s = sel[name]
        out += '  "%s":\n    strategy: %s\n    targets:\n' % (name, s["strategy"])
        for t in s["targets"]:
            out += '      - "%s"\n' % t
        if s["strategy"] == "spillover":
            n = (s.get("settings") or {}).get("spillover") or 1
            out += "    settings:\n      spillover: %d\n" % int(n)
        for k in ("name", "description"):
            if s.get(k):
                out += '    %s: "%s"\n' % (k, str(s[k]).replace('"', "'"))
    return out


def write_selectors(sel: dict, text: str | None = None) -> str:
    """Replace the roles block, creating it above 'models:' if it is absent."""
    text = config_text() if text is None else text
    head = ("# " + "=" * 76 + "\n"
            "#  ROLES (selectors)  —  virtual model names, maintained by 'llm role'\n"
            "# " + "=" * 76 + "\n"
            "#  A client asks for a ROLE and llama-swap picks a real model for it, so\n"
            "#  clients and their subagents never need to know a file name.\n"
            "#   strategy: warm      -> the first target that is already running\n"
            "#   strategy: pin       -> always the first target\n"
            "#   strategy: spillover -> target 1 up to 'spillover' concurrent requests,\n"
            "#                          then target 2 starts (= the second card)\n"
            "#  A role reports the SMALLEST context and the INTERSECTION of the\n"
            "#  capabilities of its targets.  DO NOT edit by hand:\n")
    return put_block(text, _SEL_MARK, render_selectors(sel), head)


def set_selector(name: str, strategy: str, targets: list[str],
                 spillover: int | None = None, description: str | None = None,
                 dry_run: bool = False) -> dict:
    """Create or change a role. Validates against the real model list."""
    if strategy not in SELECTOR_STRATEGIES:
        raise ValueError("strategy must be one of %s" % ", ".join(SELECTOR_STRATEGIES))
    if not targets:
        raise ValueError("a role needs at least one target model")
    text = config_text()
    known = {e["name"] for e in parse_config(text)}
    unknown = [t for t in targets if t not in known]
    if unknown:
        raise ValueError("not a configured model: %s (see llm ls)" % ", ".join(unknown))
    if name in known:
        raise ValueError("'%s' is already a model name - a role needs its own name" % name)
    sel = read_selectors(text)
    entry = {"strategy": strategy, "targets": list(targets)}
    if strategy == "spillover":
        entry["settings"] = {"spillover": int(spillover or 1)}
    if description:
        entry["description"] = description
    before = dict(sel)
    sel[name] = entry
    out = {"role": name, "before": before.get(name), "after": entry, "dryRun": dry_run}
    if not dry_run:
        with config_lock():
            _write_config(write_selectors(sel, config_text()))
    return out


def del_selector(name: str) -> dict:
    sel = read_selectors()
    if name not in sel:
        raise KeyError("no role called '%s'" % name)
    removed = sel.pop(name)
    with config_lock():
        _write_config(write_selectors(sel, config_text()))
    return {"role": name, "removed": removed}


#  The chat macros that spread a model over ALL cards. server-embed and
#  server-rerank are deliberately not in this list: embedding and reranker models
#  are small, run on one card, and never had a -ts.
_TS_MACROS = ("server", "server-mtp", "server-ngram")
_TS_LINE_RE = re.compile(r"^\s*-ts\s+[\d.,]+\s*$")
_MACRO_KEY_RE = re.compile(r'^(\s+)"?([\w.-]+)"?:\s*>\s*$')


def tensor_split_drift() -> dict:
    """What the config says about -ts versus what the hardware wants.

    One implementation on purpose. bin/llm used to carry this twice as the same
    sed one-liner, with a third regex here that additionally accepts decimal
    points - so a hand-written '-ts 1.5,1' was visible to Python and invisible
    to bash, and `llm doctor` reported a match that was not one.
    """
    try:
        lines = config_text().split("\n")
    except ConfigMissing:
        return {"configured": None, "expected": tensor_split(), "drifted": False,
                "reason": "no configuration yet"}
    have = next((m.group(1) for m in
                 (re.match(r"^\s*-ts\s+([\d.,]+)\s*$", ln) for ln in lines) if m), None)
    want = tensor_split()
    return {"configured": have, "expected": want,
            "drifted": bool((have or want) and have != want), "reason": None}


def sync_tensor_split(text: str | None = None, value: str = "auto") -> str:
    """Adjust -ts in the chat macros to the detected card count.

    value="auto" -> from tensor_split(), i.e. gone with one card and "1,..,1"
    with several.  value=""/None -> remove the line.  Anything else is used as is.

    Works LINE BY LINE: '-ts' sits on a line of its own inside the folded
    YAML scalar, and YAML joins folded lines with a space - so the resulting
    command line is the same as before (verified), without a regex having to cut
    across a multi-line scalar.
    """
    ts = tensor_split() if value == "auto" else (value or None)
    lines = (config_text() if text is None else text).splitlines(keepends=True)
    out: list[str] = []
    i, n, in_macros = 0, len(lines), False
    while i < n:
        line = lines[i]
        if re.match(r"^macros:\s*$", line):
            in_macros = True
            out.append(line)
            i += 1
            continue
        if in_macros and line.strip() and not line[:1].isspace():
            in_macros = False                     # the next section of the file
        m = _MACRO_KEY_RE.match(line) if in_macros else None
        if not m:
            out.append(line)
            i += 1
            continue
        indent, name = m.group(1), m.group(2)
        out.append(line)
        i += 1
        body: list[str] = []
        while i < n:                              # body = everything indented deeper
            cur = lines[i]
            if cur.strip() and (len(cur) - len(cur.lstrip())) <= len(indent):
                break
            body.append(cur)
            i += 1
        if name in _TS_MACROS:
            body = [b for b in body if not _TS_LINE_RE.match(b)]
            if ts:
                #  Appended at the end of the body: llama-server does not care
                #  about flag order, and this way no anchor line is needed that a
                #  hand edit could push out of place.
                tail: list[str] = []
                while body and not body[-1].strip():
                    tail.insert(0, body.pop())
                body.append("%s-ts %s\n" % (indent + "  ", ts))
                body.extend(tail)
        out.extend(body)
    return "".join(out)


def write_env() -> dict:
    """Write config/hardware.env and comfyui.env from the detection.

    Why a file rather than 'Environment=' in the unit: the card numbers belong to
    the machine, while the unit should be able to live in the repository. The
    units pull it in with 'EnvironmentFile=-'. Two files, because systemd does not
    expand variables inside 'Environment=' values and ComfyUI gets only ONE card.
    """
    info = hw()
    head = ("# GENERATED by 'llm gpu sync' - do not edit by hand.\n"
            "# The card numbers in here are ABSOLUTE (the way the backend counts) -\n"
            "# see lib/llmreg.py. LLM_BACKEND selects which backend that is.\n")
    #  The mask is written under the active backend's name and no other, so the
    #  file cannot end up carrying two of them saying different things after a
    #  switch. LLM_GFX_TARGETS and LLM_HIP_COMPILER stay in the file even when
    #  empty: lib/update.sh reads them with hw_get, and an absent key and an
    #  empty one mean the same thing there while a MISSING line reads as "this
    #  file predates the backend that needs it".
    body = "".join("%s=%s\n" % kv for kv in (
        ("LLM_BACKEND", info["backend"]),
        (info["visibleEnv"], info["hipVisibleDevices"]),
        ("LLM_GFX_TARGETS", info["gfxTargets"] or ""),
        ("LLM_HIP_COMPILER", info["hipCompiler"] or ""),
        ("LLM_TENSOR_SPLIT", info["tensorSplit"] or ""),
    ))
    #  ComfyUI holds VRAM for as long as it runs, so it only sees one card -
    #  which one is a matter of taste and settable with LLM_COMFY_GPU. Always
    #  HIP_VISIBLE_DEVICES here whatever the backend is: ComfyUI runs on a ROCm
    #  torch wheel and there is no Vulkan one, so this file is meaningless under
    #  Vulkan rather than differently spelled. docs/COMFYUI.md says so.
    comfy_logical = _num(os.environ.get("LLM_COMFY_GPU"), 0)
    comfy_smi = to_smi(comfy_logical)
    for path, text in ((HARDWARE_ENV, head + body),
                       (COMFY_ENV, head + "HIP_VISIBLE_DEVICES=%s\n" % (
                           "" if comfy_smi is None else comfy_smi))):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return {"hardwareEnv": HARDWARE_ENV, "comfyEnv": COMFY_ENV,
            "hipVisibleDevices": info["hipVisibleDevices"],
            "comfyCard": comfy_logical if comfy_smi is not None else None,
            "warnings": info["warnings"]}


def sync_device_names(text: str) -> str:
    """Rewrite both ways of naming a card into the active backend's spelling.

    Two rewrites, and the second one is not cosmetic:

    * '--device ROCm0' -> '--device Vulkan0' (and --spec-draft-device with it).
      gpu_of() reads either prefix, so nothing breaks without this - but the file
      would keep saying ROCm on a Vulkan machine, and that lie survives into
      every diff and every screenshot.
    * 'HIP_VISIBLE_DEVICES=1' -> 'GGML_VK_VISIBLE_DEVICES=1' in a model's env:.
      This one changes behaviour. It is how whisper gets its card - it has no
      --device flag - and a mask the runtime does not read is not a mask: the
      entry would spread over every card instead of the one it names.

    The numbers are untouched. The prefix takes the LOGICAL card and the mask the
    ABSOLUTE one, and neither meaning depends on the backend.
    """
    mine, mine_env = device_prefix(), visible_env()
    others = [p for p in _ALL_DEVICE_PREFIXES if p != mine]
    other_envs = [v for v in _ALL_VISIBLE_ENVS if v != mine_env]
    if others:
        pat = re.compile(r"(--device\s+|--spec-draft-device\s+)(?:%s)(\d+)"
                         % "|".join(re.escape(p) for p in others))
        text = pat.sub(lambda m: "%s%s%s" % (m.group(1), mine, m.group(2)), text)
    if other_envs:
        #  Only in an env: list item, so a mask named in a comment or in a cmd
        #  line is left alone. The quotes are optional in the pattern and
        #  preserved in the replacement: parse_config() reads ONLY the quoted
        #  form (`- "VAR=1"`), which is what everything in this project writes,
        #  but a hand-edited config without them should still be renamed rather
        #  than silently skipped - it was skipped by the first version of this,
        #  which is why the quotes are in the pattern at all.
        pat = re.compile(r'^(\s*-\s*"?)(?:%s)(=[\d,]+"?\s*)$'
                         % "|".join(re.escape(v) for v in other_envs), re.M)
        text = pat.sub(lambda m: "%s%s%s" % (m.group(1), mine_env, m.group(2)), text)
    return text


def _SMI_CACHE_RESET() -> None:
    """Forget the cached card list, e.g. after the backend changed."""
    global _SMI_CACHE
    _SMI_CACHE = None


def gpu_sync(dry_run: bool = False) -> dict:
    """Bring configuration and environment files in line with the hardware.

    Collects everything that can drift apart after a card change or a backend
    switch: the GPU groups, the -ts in the chat macros, the device prefix in
    every pinned model's cmd, and the visible-devices mask.
    """
    old = config_text()
    new = sync_tensor_split(sync_groups(sync_device_names(old)))
    changed = new != old
    out = {"configChanged": changed, "tensorSplit": tensor_split(),
           "cards": gpu_count(), "backend": backend_name(), "dryRun": dry_run}
    if dry_run:
        import difflib
        out["diff"] = "\n".join(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile="llama-swap.yaml", tofile="llama-swap.yaml (new)", lineterm=""))
        return out
    if changed:
        with config_lock():
            _write_config(new)
    out.update(write_env())
    return out


def set_gpu(cmd: str, mode, is_mtp: bool) -> str:
    """Set or remove --device/-sm/-mg (+ draft card); 'both' = every card."""
    for f in ("--device", "-sm", "-mg", "--spec-draft-device"):
        cmd = del_flag(cmd, f)
    if mode != "both":
        dev = device_prefix()
        cmd = _tidy("%s --device %s%s -sm none -mg 0" % (cmd, dev, mode))
        if is_mtp:
            # An MTP drafter needs the card stated SEPARATELY or llama-server aborts.
            cmd = _tidy("%s --spec-draft-device %s%s" % (cmd, dev, mode))
    return cmd


def check_fit(model: dict, ctx: int | None = None, gpu=None) -> dict:
    """Does the model fit on the target card(s)? {ok, needBytes, freeBytes, reason}.

    The point when spreading over several cards: what counts is the share PER
    CARD, not the sum. With an even -ts a 30 GB model needs 15 GB on each of two
    cards - if one of them already holds something, plenty of free space on the
    other does not help and the load runs out of memory. This used to sum the
    free VRAM and therefore reported "fits".
    """
    gpu = model["runtime"]["gpu"]["device"] if gpu is None else gpu
    weights = model["vram"]["weightsBytes"] or 0
    ctx = ctx if ctx is not None else model["runtime"]["contextWindow"]
    kv = kv_cache_bytes(model["files"].get("model", {}).get("path", ""), ctx,
                        model["runtime"]["kvCacheQuant"],
                        model["runtime"].get("parallel") or 1) or 0
    need = vram_needed(weights, kv) or 0
    #  If the model is already running, it occupies the space we are computing.
    loaded = model.get("state") in ("ready", "loaded")
    all_cards = gpus()
    if gpu == "both" or gpu is None:
        cards = all_cards
        label = "all %d cards" % len(cards) if len(cards) != 1 else "card 0"
    else:
        cards = [c for c in all_cards if c["index"] == int(gpu)]
        label = "card %s" % gpu
    if not cards:
        return {"ok": True, "needBytes": need, "freeBytes": None, "target": label,
                "kvCacheBytes": kv, "weightsBytes": weights, "perCard": [], "reason": None}
    #  A card whose VRAM size is not known is not a card with zero VRAM. Under
    #  Vulkan the figures come from amdgpu's sysfs, and a card on any other
    #  driver has none - so treating absent as empty would refuse every model on
    #  an Intel or NVIDIA card while claiming it has "0.0 GB free". Same rule as
    #  no cards at all: no refusal, and say why rather than answer a number that
    #  was never measured.
    if any(not c.get("vramTotalBytes") for c in cards):
        return {"ok": True, "needBytes": need, "freeBytes": None, "target": label,
                "kvCacheBytes": kv, "weightsBytes": weights, "perCard": [],
                "reason": "needs about %.1f GB; free VRAM is not readable for %s "
                          "on this driver, so the fit was not checked"
                          % (need / 1024**3, label)}
    #  An even split, exactly as the generated -ts prescribes.
    share = need / len(cards)
    per, tight = [], None
    for c in cards:
        free = c["vramFreeBytes"] + ((weights + kv) / len(cards) if loaded else 0)
        per.append({"card": c["index"], "freeBytes": int(free), "needBytes": int(share)})
        if free < share and (tight is None or free < tight["freeBytes"]):
            tight = per[-1]
    free_total = sum(p["freeBytes"] for p in per)
    ok = tight is None
    return {"ok": ok, "needBytes": need, "freeBytes": free_total, "target": label,
            "kvCacheBytes": kv, "weightsBytes": weights, "perCard": per,
            "reason": None if ok else
            #  The "per card" clause only with several cards - with one it would
            #  be the same number twice.
            ("needs about %.1f GB on %s%s - card %d has only %.1f GB free"
             % (need / 1024**3, label,
                ", i.e. %.1f GB per card" % (share / 1024**3) if len(cards) > 1 else "",
                tight["card"], tight["freeBytes"] / 1024**3))}


def patch_model(name: str, changes: dict, dry_run: bool = False) -> dict:
    """Change a model's runtime configuration. Returns before/after.

    changes: gpu (0|1|'both'), contextWindow, parallel (slots), ttl,
             sampling {..}, extraFlags (str), piOverrides {key: value|None},
             force (bool)
    """
    if dry_run:                                   # compute only, touch nothing
        return _patch_model(name, changes, True)
    with config_lock():
        return _patch_model(name, changes, False)


def _patch_model(name: str, changes: dict, dry_run: bool) -> dict:
    text = config_text()
    m = find_block(name, text)
    if not m:
        raise KeyError("model '%s' is not in the configuration" % name)
    body = m.group(1)
    entry = next((e for e in parse_config(text) if e["name"] == name), None)
    if entry is None:
        raise KeyError("the block for '%s' has no cmd line" % name)
    before = get_model(name)
    cmd_old = entry["cmd"]
    cmd = cmd_old
    body_new = body
    notes = []

    if "contextWindow" in changes and changes["contextWindow"] is not None:
        cmd = set_flag(cmd, "-c", int(changes["contextWindow"]))

    if "parallel" in changes and changes["parallel"] is not None:
        np_ = int(changes["parallel"])
        if np_ < 1:
            raise ValueError("parallel must be >= 1")
        if before["role"] == "stt":
            raise ValueError("whisper-server has no slots")
        # Normalise both spellings away first, then write one canonical form.
        for f in ("--parallel", "-np"):
            cmd = del_flag(cmd, f)
        for f in ("-kvu", "--kv-unified", "-no-kvu", "--no-kv-unified"):
            cmd = del_flag(cmd, f, with_value=False)   # bare switches
        cmd = set_flag(cmd, "-np", np_)
        # Shared KV pool: one long request may still use the whole -c.
        cmd = set_flag(cmd, "-kvu", None)
        notes.append("%d slots, shared KV pool" % np_)

    if "gpu" in changes and changes["gpu"] is not None:
        gpu = changes["gpu"]
        gpu = "both" if str(gpu) == "both" else int(gpu)
        if before["role"] == "stt":
            # Whisper gets its card through the environment, not through flags -
            # and the mask counts ABSOLUTE cards the way the backend does, while
            # `gpu` here is the logical index. Writing the logical one addresses
            # the wrong card on any machine whose iGPU does not sort last.
            # gpu_of() reads this back through to_logical().
            if gpu == "both":
                raise ValueError("whisper always runs on exactly one card")
            smi = to_smi(gpu)
            if smi is None:
                raise ValueError("card %d is not a compute card (llm gpu list)" % gpu)
            #  Rewrite whichever spelling is in the entry rather than the active
            #  backend's, so a config written under ROCm keeps working after a
            #  switch instead of gaining a second, contradictory mask. Renaming
            #  it is 'llm gpu sync'.
            var = next((v for v in _ALL_VISIBLE_ENVS if v + "=" in body_new), visible_env())
            body_new, hits = re.subn(r'(%s=)\d+' % var, r"\g<1>%d" % smi, body_new)
            if not hits:
                raise ValueError(
                    "this entry pins no card through the environment - expected a "
                    "'%s=<n>' line under env:" % var)
            notes.append("env %s=%d (logical card %d)" % (var, smi, gpu))
        else:
            cmd = set_gpu(cmd, gpu, is_mtp=before["runtime"]["specDecoding"] == "mtp")

    for key, fl in (("temperature", "--temp"), ("top_p", "--top-p"),
                    ("top_k", "--top-k"), ("min_p", "--min-p")):
        val = (changes.get("sampling") or {}).get(key, "__keep__")
        if val == "__keep__":
            continue
        cmd = del_flag(cmd, fl) if val is None else set_flag(cmd, fl, val)

    if changes.get("extraFlags"):
        cmd = _tidy(cmd + " " + changes["extraFlags"])

    if cmd != cmd_old:
        body_new = body_new.replace('cmd: "%s"' % cmd_old, 'cmd: "%s"' % cmd, 1)

    if "ttl" in changes and changes["ttl"] is not None:
        body_new = re.sub(r"^(\s*ttl:\s*)\d+", r"\g<1>%d" % int(changes["ttl"]),
                          body_new, count=1, flags=re.M)

    for key, val in (changes.get("piOverrides") or {}).items():
        line = "  # pi: %s=%s\n" % (key, val)
        pat = re.compile(r"^\s*# pi:\s*%s\s*=.*\n" % re.escape(key), re.M)
        if pat.search(body_new):
            body_new = pat.sub("" if val is None else line, body_new)
        elif val is not None:
            body_new = body_new.rstrip("\n") + "\n" + line
        notes.append("pi override %s" % key)

    # Does it still fit on the card? Evaluate the PATCHED command, not `before`:
    # -ctk halves the KV cache and -np changes the slot count, so checking the
    # old values could refuse a change that in fact frees room.
    target_gpu = changes.get("gpu", before["runtime"]["gpu"]["device"]
                             if before["runtime"]["gpu"]["mode"] == "single" else "both")
    np_new, _ = slots_of(cmd, before["role"])
    candidate = dict(before, runtime=dict(before["runtime"],
                                          kvCacheQuant=flag(cmd, "-ctk"),
                                          parallel=np_new))
    fit = check_fit(candidate, ctx=_num(changes.get("contextWindow"),
                                        _num(flag(cmd, "-c"),
                                             before["runtime"]["contextWindow"])),
                    gpu=target_gpu)
    if not fit["ok"] and not changes.get("force"):
        raise MemoryError(fit["reason"])

    result = {"model": name, "changed": body_new != body, "fit": fit, "notes": notes,
              "before": {"cmd": cmd_old, "ttl": entry["ttl"]},
              "after": {"cmd": cmd, "ttl": _num(changes.get("ttl"), entry["ttl"])}}
    if dry_run or not result["changed"]:
        return result

    text = text[:m.start(1)] + body_new + text[m.end(1):]
    text = sync_groups(text)
    _write_config(text)
    result["reloaded"] = reload_swap()
    return result


def remove_model(name: str, delete_files: bool = False) -> dict:
    """Remove a model, its group membership and any role that pointed at it."""
    dropped_roles = []
    with config_lock():
        text = config_text()
        if not find_block(name, text):
            raise KeyError("model '%s' is not in the configuration" % name)
        model = get_model(name)
        text = re.sub(r"\n*# >>> llm:%s\n.*?# <<< llm:%s\n" % (re.escape(name), re.escape(name)),
                      "\n", text, flags=re.S)
        #  A role whose target no longer exists is an invalid configuration -
        #  llama-swap validates the targets at startup, so leaving one behind
        #  would take the whole endpoint down on the next restart.
        sel = read_selectors(text)
        touched = False
        for role, spec in list(sel.items()):
            if name not in spec["targets"]:
                continue
            spec["targets"] = [t for t in spec["targets"] if t != name]
            touched = True
            if not spec["targets"]:
                del sel[role]
                dropped_roles.append(role)
        if touched:
            text = write_selectors(sel, text)
        text = sync_groups(text)
        _write_config(text)
    removed = []
    if delete_files:
        path = (model or {}).get("files", {}).get("model", {}).get("path")
        d = os.path.dirname(path) if path else os.path.join(MODELS, name)
        inside = os.path.realpath(d).startswith(os.path.realpath(MODELS) + os.sep)
        if d and os.path.isdir(d) and inside:
            shutil.rmtree(d)
            removed.append(d)
    return {"model": name, "filesRemoved": removed, "rolesRemoved": dropped_roles,
            "reloaded": reload_swap()}


def reload_swap() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE], timeout=10)
        if r.returncode != 0:
            return False
        subprocess.run(["systemctl", "--user", "restart", SERVICE], timeout=60, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def load_model(name: str, timeout: float = 300.0) -> dict:
    """Pull a model into VRAM - llama-swap loads it on the first request."""
    m = get_model(name)
    if not m:
        raise KeyError(name)
    t0 = time.time()
    if m["role"] == "stt":
        raise ValueError("whisper models only load on a real audio request")
    if m["role"] == "embed":
        url, payload = SWAP_API + "/v1/embeddings", {"model": name, "input": "ping"}
    elif m["role"] == "rerank":
        url, payload = SWAP_API + "/v1/rerank", {"model": name, "query": "ping",
                                                 "documents": ["ping"]}
    else:
        url, payload = SWAP_API + "/v1/chat/completions", {
            "model": name, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    _http_json(url, timeout=timeout, method="POST",
               data=json.dumps(payload).encode(),
               headers={"Content-Type": "application/json"})
    return {"model": name, "state": "ready", "seconds": round(time.time() - t0, 1)}


def unload_all() -> dict:
    try:
        urllib.request.urlopen(SWAP_API + "/unload", timeout=30).read()
        return {"unloaded": True}
    except Exception as exc:      # noqa: BLE001 - the caller wants the reason, not a raise
        return {"unloaded": False, "error": str(exc)}


# ---------------------------------------------------------------------------
#  Filling in the provenance afterwards
# ---------------------------------------------------------------------------
KNOWN_PUBLISHERS = ["unsloth", "bartowski", "ggml-org", "Qwen", "google", "mradermacher",
                    "ggerganov", "primeline", "lmstudio-community"]


def backfill_candidates(dirname: str) -> list[str]:
    """Candidate repos for a model directory, most likely first."""
    base = re.sub(r"^mmproj-", "", dirname)
    base = re.sub(r"-(ud-)?(i?q\d[^-]*|bf16|f16)(_[a-z0-9]+)*$", "", base, flags=re.I)
    base = re.sub(r"-ud$", "", base)
    variants = {base, base.upper(), base.replace("-mtp", "")}
    cands = []
    if base.startswith("whisper"):
        # whisper.cpp models are .bin files in collection repos, not GGUF
        cands += ["ggerganov/whisper.cpp", "ggml-org/whisper.cpp"]
    for v in list(variants):
        for pub in KNOWN_PUBLISHERS:
            cands.append("%s/%s-GGUF" % (pub, v))
    for v in list(variants):
        cands += hf_search(v.replace("-", " "), limit=15)
        if base.startswith("whisper"):
            cands += hf_search(v.replace("-", " "), limit=15, gguf_only=False)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def backfill(dirname: str, repo: str | None = None, quant: str | None = None,
             verify: bool = True) -> dict:
    """Work out the provenance of an existing model directory and store it."""
    d = os.path.join(MODELS, dirname)
    if not os.path.isdir(d):
        raise KeyError(dirname)
    sha = hf_commit(d)
    result = {"dir": dirname, "revision": sha, "repo": None, "verified": False}
    if repo:
        canon = hf_verify(repo, sha) if (verify and sha) else None
        result["repo"] = canon or repo
        result["verified"] = bool(canon)
    elif sha:
        for cand in backfill_candidates(dirname):
            canon = hf_verify(cand, sha)
            if canon:
                result["repo"] = canon
                result["verified"] = True
                break
    if not quant:
        gguf = [f for f in os.listdir(d) if f.endswith((".gguf", ".bin"))]
        m = re.search(r"-(UD-)?([IQ]Q?\d[^.]*?|BF16|F16)\.(gguf|bin)$", gguf[0]) if gguf else None
        quant = m.group(2) if m else None
    if result["repo"]:
        files = []
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p) and f.endswith((".gguf", ".bin")):
                files.append({"name": f, "sizeBytes": os.path.getsize(p),
                              "sha256": file_digest(p)})
        write_meta(d, {"repo": result["repo"], "revision": sha, "quant": quant,
                       "files": files, "source": "backfill" if not repo else "manuell",
                       "verified": result["verified"],
                       "addedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(os.path.getmtime(d)))})
    return result


def record_add(dirname: str, repo: str, quant: str, extra: dict | None = None) -> str:
    """Called by 'llm add': record the provenance right at download time."""
    d = os.path.join(MODELS, dirname)
    files = []
    for root, _dirs, fs in os.walk(d):
        if ".cache" in root:
            continue
        for f in sorted(fs):
            if f.endswith((".gguf", ".bin")):
                p = os.path.join(root, f)
                files.append({"name": os.path.relpath(p, d), "sizeBytes": os.path.getsize(p),
                              "sha256": file_digest(p)})
    data = {"repo": repo, "quant": quant, "revision": hf_commit(d), "files": files,
            "source": "llm add", "verified": True,
            "addedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    data.update(extra or {})
    return write_meta(d, data)


# ---------------------------------------------------------------------------
#  Engine versions and what there is to roll back to
# ---------------------------------------------------------------------------
#  Read here rather than in bash so the CLI and the HTTP API cannot disagree -
#  the same reason the card list moved. The update MACHINERY stays in
#  lib/update.sh; this is only the reporting side of it.
UPDATE_CACHE = os.path.join(LLM_HOME, ".update-cache")
UPDATE_STATE = os.path.join(LLM_HOME, ".update-state")


def _kv_file(path: str) -> dict:
    """A 'key=value' file, or {} when it is missing."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.partition("=")
                if v:
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _git_out(repo: str, *args: str) -> str | None:
    """One reading git call in a repository, or None. Never fetches."""
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def engine_versions() -> dict:
    """Active version, installed alternatives and rollback command per engine.

    'latest' comes from .update-cache, which 'llm update' refreshes in the
    background - never from a live GitHub call, because this is also what
    `llm status` prints and it must not block on the network.
    """
    latest = _kv_file(UPDATE_CACHE)
    prev = _kv_file(UPDATE_STATE)
    out = {}

    def same_commit(active, latest_key, repo):
        """Up to date can also mean: another name for the same commit.

        whisper.cpp publishes one commit as bNNNN and as v1.x.y, and GitHub's
        releases/latest answers with whichever was published last, so comparing
        the names alone reports an update that does not exist. lib/update.sh
        caches the commit each 'latest' tag points at as "<tag> <sha>", so the
        entry cannot outlive the tag it belongs to. Unknown on either side means
        we do not know, and the caller keeps the name comparison.
        """
        rec = (latest.get(latest_key + "_sha") or "").split(" ")
        if len(rec) != 2 or rec[0] != latest.get(latest_key):
            return False
        return _git_out(repo, "rev-parse", "--verify", "--quiet",
                        "%s^{commit}" % active) == rec[1]

    def entry(active, installed, latest_key, rollback, repo=None):
        #  'repo' marks the git checkouts, the only ones where two tag names can
        #  mean one commit. Without it this stays the strict name comparison the
        #  CLI uses for llama-swap and Open WebUI.
        others = [v for v in installed if v != active]
        want = latest.get(latest_key)
        if not (active and want):
            up = None
        elif repo is None:
            up = active == want
        else:
            up = (active.removeprefix("v") == want.removeprefix("v")
                  or same_commit(active, latest_key, repo))
        return {"active": active, "latest": want, "upToDate": up,
                "rollbackTo": others, "rollbackCommand": rollback}

    #  llama.cpp and whisper.cpp: one build directory per version, 'build' a
    #  symlink to the active one, so switching back is a symlink change.
    whisper_root = (os.environ.get("LLM_WHISPER_HOME")
                    or os.path.join(os.path.expanduser("~"), "whisper.cpp"))
    for key, root, cmd in (("llamaCpp", os.path.join(LLM_HOME, "llama.cpp"), "llm rollback llama"),
                           ("whisperCpp", whisper_root, "llm rollback whisper")):
        real = os.path.realpath(root)
        active = os.path.basename(os.path.realpath(os.path.join(real, "build"))) \
            .replace("build-", "") or None
        builds = sorted(os.path.basename(d).replace("build-", "")
                        for d in glob.glob(os.path.join(real, "build-*"))
                        if os.path.isdir(d))
        out[key] = entry(active if active and active != "build" else None, builds,
                         "llama" if key == "llamaCpp" else "whisper", cmd, real)

    #  llama-swap: prebuilt binaries kept side by side as bin/llama-swap-<ver>.
    swap_active = None
    try:
        r = subprocess.run([os.path.join(LLM_HOME, "bin", "llama-swap"), "--version"],
                           capture_output=True, text=True, timeout=10)
        swap_active = (r.stdout or "").split("\n")[0].replace("version: ", "").strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    if swap_active:
        swap_active = swap_active.split(" ")[0]
    swaps = sorted(os.path.basename(f).replace("llama-swap-", "")
                   for f in glob.glob(os.path.join(LLM_HOME, "bin", "llama-swap-*")))
    out["llamaSwap"] = entry(swap_active, swaps, "swap", "llm rollback swap")

    #  Open WebUI and ComfyUI: the previous version is remembered in
    #  .update-state, and a matching data snapshot may sit next to it.
    ui_active = None
    try:
        r = subprocess.run([os.path.join(LLM_HOME, "venv-webui", "bin", "python"), "-c",
                            "import importlib.metadata as m; print(m.version('open-webui'))"],
                           capture_output=True, text=True, timeout=20)
        ui_active = (r.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    ui_prev = prev.get("owui_prev")
    out["openWebUI"] = entry(ui_active, [v for v in (ui_active, ui_prev) if v], "ui",
                             "llm rollback ui")
    out["openWebUI"]["snapshotWithDatabase"] = bool(ui_prev and os.path.isdir(
        os.path.join(LLM_HOME, "openwebui-data.bak-%s" % ui_prev)))
    #  Same order as comfy_active in lib/update.sh: the exact tag when HEAD sits
    #  on one, otherwise the version the checkout declares. Reported as None for
    #  a while, which left the version table with nothing to compare.
    comfy_root = os.path.realpath(os.environ.get("LLM_COMFY_HOME")
                                  or os.path.join(os.path.expanduser("~"), "comfyui"))
    comfy_active = None
    if os.path.isdir(os.path.join(comfy_root, ".git")):
        comfy_active = _git_out(comfy_root, "describe", "--tags", "--exact-match", "HEAD")
        if not comfy_active:
            try:
                with open(os.path.join(comfy_root, "comfyui_version.py"), encoding="utf-8") as fh:
                    m = re.search(r'^__version__ = "(.*)"', fh.read(), re.M)
                comfy_active = m.group(1) if m else None
            except OSError:
                pass
    out["comfyUI"] = entry(comfy_active,
                           [v for v in (comfy_active, prev.get("comfy_prev")) if v],
                           "comfy", "llm rollback comfy", comfy_root)
    #  The backend belongs with the engine versions rather than in a ninth call
    #  from the control page: it is what the engines were BUILT for, and a build
    #  for the other one is exactly the thing the System tab should show.
    return {"llmBox": _read_version(), "engines": out, "backend": backend_name(),
            "cacheAgeSeconds": int(time.time() - os.path.getmtime(UPDATE_CACHE))
            if os.path.exists(UPDATE_CACHE) else None}


def _read_version() -> str:
    try:
        with open(os.path.join(LLM_HOME, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


def config_overview() -> dict:
    """The parts of llama-swap.yaml that are NOT per-model: macros and groups.

    The eviction semantics live there - swap, exclusive, persistent - and the
    config comments spend ten lines explaining them, which is no help to anyone
    who is not looking at the file. llama-swap has no /api/config at all.
    """
    text = config_text()
    macros, cur = {}, None
    in_macros = False
    for line in text.split("\n"):
        if re.match(r"^macros:\s*$", line):
            in_macros = True
            continue
        if in_macros and line.strip() and not line[:1].isspace():
            break
        if not in_macros:
            continue
        m = re.match(r'^  "?([\w.-]+)"?:\s*>\s*$', line)
        if m:
            cur = m.group(1)
            macros[cur] = []
        elif cur and line.startswith("    "):
            macros[cur].append(line.strip())
    groups = {}
    m = re.search(r"# >>> llm:groups\n(.*?)# <<< llm:groups", text, re.S)
    if m:
        name = None
        for line in m.group(1).split("\n"):
            g = re.match(r"^  ([\w-]+):\s*$", line)
            if g:
                name = g.group(1)
                groups[name] = {"swap": None, "exclusive": None, "persistent": None,
                                "members": []}
                continue
            if not name:
                continue
            kv = re.match(r"^    (swap|exclusive|persistent):\s*(true|false)\s*$", line)
            if kv:
                groups[name][kv.group(1)] = kv.group(2) == "true"
            mem = re.match(r'^      - "([^"]+)"', line)
            if mem:
                groups[name]["members"].append(mem.group(1))
    return {"path": CONFIG, "macros": {k: " ".join(v) for k, v in macros.items()},
            "groups": groups, "healthCheckTimeout":
            _num((re.search(r"^healthCheckTimeout:\s*(\d+)", text, re.M) or [None, None])[1], None)}


# ---------------------------------------------------------------------------
#  The token for write access
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#  The API key for the inference endpoint (port 8080)
# ---------------------------------------------------------------------------
#  Separate from the registry token on purpose: this one is handed to every
#  client that wants inference, the other one guards changing the configuration.
#
#  Why it exists at all: llama-swap is default-allow, so on a machine whose 8080
#  is reachable, GET /unload empties the VRAM with no authentication and no
#  confirmation. Being a GET it does not even take a person - a browser prefetch
#  or a link checker is enough, which is how it was found here. An apiKeys entry
#  closes that, and /ui, /logs and /metrics with it, while leaving /health open
#  so reachability checks still work.
API_KEY_FILE = os.path.join(LLM_HOME, "config", "api-key")
_APIKEY_MARK = "apikeys"


def api_key(create: bool = False) -> str | None:
    """The inference key, or None when the endpoint is left open.

    Unlike api_token this does NOT create one on demand: switching the endpoint
    to authenticated breaks every client that does not know the key yet, so it
    has to be a deliberate act ('llm key new').
    """
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    if not create:
        return None
    key = "sk-" + secrets.token_urlsafe(36)
    os.makedirs(os.path.dirname(API_KEY_FILE), exist_ok=True)
    with open(API_KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    os.chmod(API_KEY_FILE, 0o600)
    return key


def drop_api_key() -> bool:
    """Remove the key file. Returns whether there was one."""
    if not os.path.exists(API_KEY_FILE):
        return False
    os.remove(API_KEY_FILE)
    return True


API_KEY_ENV = os.path.join(LLM_HOME, "config", "api-key.env")


def sync_api_key(text: str | None = None) -> str:
    """Write or remove the apiKeys block, following the key file.

    A marker block rather than a '${env.VAR}' reference, so what is in force is
    visible in the file - and because llama-swap rejects an empty key, which is
    what an unset variable would expand to.

    Also refreshes config/api-key.env, which the Open WebUI unit reads through
    EnvironmentFile - so rotating the key is 'llm key new' plus a restart, and
    never a re-render of the unit.
    """
    text = config_text() if text is None else text
    key = api_key()
    try:
        os.makedirs(os.path.dirname(API_KEY_ENV), exist_ok=True)
        with open(API_KEY_ENV, "w", encoding="utf-8") as fh:
            fh.write("#  Generated by 'llm key'. The chat UI reads this; the value is\n"
                     "#  the same one in config/api-key.\n"
                     "OPENAI_API_KEY=%s\n" % (key or "sk-local"))
        os.chmod(API_KEY_ENV, 0o600)
    except OSError as exc:
        #  Not swallowed: if this file cannot be written the chat UI keeps
        #  offering the previous key and every request through it starts failing
        #  401 after the next restart, with nothing anywhere saying why.
        #
        #  Two deliberate awkwardnesses. The path is written out instead of
        #  formatting API_KEY_ENV into the message, because
        #  py/clear-text-logging-sensitive-data reads a name containing "key" as
        #  a secret and a constant path is not one. And the reason comes from
        #  errno rather than from the exception, which is narrower and reads the
        #  same. Neither form could ever have carried the key - an OSError holds
        #  errno, strerror and filename - but a warning nobody has to reason
        #  about is worth two lines.
        sys.stderr.write("warning: could not write config/api-key.env (%s) - the "
                         "chat UI will keep using its previous key\n"
                         % (os.strerror(exc.errno) if exc.errno else type(exc).__name__))
    if not key:
        return put_block(text, _APIKEY_MARK, "")
    head = ("# " + "=" * 76 + "\n"
            "#  API KEY  —  maintained by 'llm key'\n"
            "# " + "=" * 76 + "\n"
            "#  With this present, every path except /health needs\n"
            "#    Authorization: Bearer <key>\n"
            "#  including /unload, /ui, /logs and /metrics. Without it llama-swap\n"
            "#  is default-allow and a plain GET /unload frees the VRAM.\n"
            "#  The key lives in config/api-key (mode 600, not in the repo).\n"
            "#  DO NOT edit by hand - 'llm key new' rotates, 'llm key off' removes:\n")
    return put_block(text, _APIKEY_MARK, 'apiKeys:\n  - "%s"\n' % key, head)


def api_token(create: bool = True) -> str | None:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    if not create:
        return None
    tok = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return tok


# ---------------------------------------------------------------------------
#  CLI (used by bin/llm)
# ---------------------------------------------------------------------------
def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "catalog"
    if cmd == "catalog":
        print(json.dumps(catalog(), indent=2, ensure_ascii=False))
    elif cmd == "pi-json":
        print(json.dumps(pi_models_json(), indent=2, ensure_ascii=False))
    elif cmd == "api-key":                          # api-key [new|off]
        sub = argv[1] if len(argv) > 1 else "show"
        if sub == "new":
            drop_api_key()
            key = api_key(create=True)
            with config_lock():
                _write_config(sync_api_key(config_text()))
            print(key)
        elif sub == "off":
            had = drop_api_key()
            with config_lock():
                _write_config(sync_api_key(config_text()))
            print("removed" if had else "there was none")
        else:
            key = api_key()
            print(key if key else "")
    elif cmd == "remove-model":                     # remove-model <name> [true]
        name = argv[1]
        want_files = len(argv) > 2 and argv[2].lower() in ("1", "true", "yes")
        try:
            out = remove_model(name, delete_files=want_files)
        except KeyError:
            sys.exit("model '%s' not found. (llm ls)" % name)
        print("removed from the configuration: %s" % name)
        for d in out["filesRemoved"]:
            print("  files deleted: %s" % d)
        if out["rolesRemoved"]:
            print("  roles removed with it: %s" % ", ".join(out["rolesRemoved"]))
        if not out["reloaded"]:
            print("  llama-swap was not reachable - restart it with: llm restart")
    elif cmd == "ts-drift":
        d = tensor_split_drift()
        print("%s\t%s\t%s" % (d["configured"] or "", d["expected"] or "",
                               "drift" if d["drifted"] else "ok"))
    elif cmd == "sync-groups":
        _write_config(sync_groups())
    elif cmd == "selectors":                        # selectors
        print(json.dumps(read_selectors(), indent=2))
    elif cmd == "selector-set":                     # selector-set <name> <strategy> <target>...
        rest = [a for a in argv[1:] if not a.startswith("--")]
        spill = _num(next((a.split("=", 1)[1] for a in argv
                           if a.startswith("--spillover=")), None), None)
        desc = next((a.split("=", 1)[1] for a in argv
                     if a.startswith("--description=")), None)
        if len(rest) < 3:
            sys.exit("usage: selector-set <name> <strategy> <target>... "
                     "[--spillover=N] [--description=...]")
        print(json.dumps(set_selector(rest[0], rest[1], rest[2:], spillover=spill,
                                      description=desc,
                                      dry_run="--dry-run" in argv), indent=2))
    elif cmd == "selector-rm":                      # selector-rm <name>
        print(json.dumps(del_selector(argv[1]), indent=2))
    elif cmd == "gpus":
        if "--table" in argv:
            #  One source for the display: bin/llm had the same rocm-smi parse a
            #  second time in awk, with its own iGPU rule and a cap at 8 cards.
            cards = gpus()
            if not cards:
                tool = "rocm-smi" if backend_name() == "rocm" else "vulkaninfo"
                print("  no compute card detected (is %s there? groups render/video?)" % tool)
            #  Every field is labelled rather than columnar: this block is
            #  printed inside 'llm status' and by 'llm gpu list', where a header
            #  row would be one more thing to scroll past. A missing sensor is
            #  '?' and not a zero, because zero watts is a claim.
            for c in cards:
                tot = (c.get("vramTotalBytes") or 0) / 1024**3
                use = (c.get("vramUsedBytes") or 0) / 1024**3
                pin = ", ".join(c.get("pinnedModels") or [])
                def q(key, card=c):
                    return "?" if card.get(key) is None else ("%g" % card[key])
                print("  card %d  junction %4s°C  %4s W  busy %3s %%  "
                      "VRAM %.1f/%.0f GB  %s%s" % (
                          c["index"], q("tempJunctionC"), q("powerW"),
                          q("busyPercent"), use, tot,
                          c.get("name") or "", "  [%s]" % pin if pin else ""))
        else:
            print(json.dumps(gpus(), indent=2))
    elif cmd == "hw":
        print(json.dumps(hw(), indent=2))
    elif cmd == "backend":                          # backend [rocm|vulkan]
        #  Reading prints the active one. Writing goes through gpu_sync, because
        #  the choice changes the device prefix in every pinned model's cmd line
        #  and the name of the mask in hardware.env - setting the variable alone
        #  would leave the configuration talking about the other backend.
        want = next((a for a in argv[1:] if not a.startswith("-")), None)
        if want is None:
            print(backend_name())
        elif want not in BACKENDS:
            sys.stderr.write("unknown backend '%s' - one of: %s\n"
                             % (want, ", ".join(BACKENDS)))
            return 2
        else:
            mod = gpu_vulkan if want == "vulkan" else gpu_rocm
            if not mod.available() and "--force" not in argv:
                sys.stderr.write(mod.missing_hint() + "\n")
                return 1
            os.environ["LLM_BACKEND"] = want
            _SMI_CACHE_RESET()
            print(json.dumps(dict(gpu_sync(), backend=want), indent=2, ensure_ascii=False))
    elif cmd == "gpu-sync":                         # gpu-sync [--dry-run]
        r = gpu_sync(dry_run="--dry-run" in argv)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif cmd == "token":
        print(api_token())
    elif cmd == "record-add":                       # record-add <dir> <repo> <quant>
        print(record_add(argv[1], argv[2], argv[3]))
    elif cmd == "backfill":                         # backfill [--force] [dir] [repo]
        args = [a for a in argv[1:] if a != "--force"]
        force = "--force" in argv
        dirs = [args[0]] if args else sorted(
            d for d in os.listdir(MODELS) if os.path.isdir(os.path.join(MODELS, d)))
        repo = args[1] if len(args) > 1 else None
        for d in dirs:
            if read_meta(os.path.join(MODELS, d)) and not repo and not force:
                print("  %-34s already recorded" % d)
                continue
            try:
                r = backfill(d, repo=repo)
            except KeyError:
                print("  %-34s not a directory" % d)
                continue
            mark = "" if r["verified"] else "  (NOT verified)"
            print("  %-34s %s%s" % (d, r["repo"] or "not resolved%s" % (
                " - no commit in the cache" if not r["revision"] else ""), mark))
    elif cmd == "meta":                             # meta <dir>
        print(json.dumps(read_meta(os.path.join(MODELS, argv[1])), indent=2, ensure_ascii=False))
    else:
        print("unknown: %s" % cmd, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    # Without this a Python traceback appears as soon as the output is piped into
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(_cli(sys.argv[1:]))
