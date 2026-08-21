"""Shared fixtures for the unit tests.

These test lib/llmreg.py directly, which the bash suites can only do through
`print()` and a string comparison. Everything that owns a filesystem path in
llmreg derives from LLM_HOME **at import time**, so a test that wants its own
directory has to import the module fresh - which is what the `reg` fixture does.
That is also why these are unit tests rather than more bash: reimporting a module
per test is not something a shell harness does well.
"""
import importlib
import os
import struct
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(REPO, "lib")

MINIMAL_CONFIG = """\
healthCheckTimeout: 300
logLevel: info

macros:
  server: >
    /bin/llama-server
    --host 127.0.0.1 --port ${PORT}
    -ngl 99 -fa on --no-webui --jinja
    -ts 1,1

# ============================================================================
#  MODELS  ('models' stays the LAST section)
# ============================================================================
models:
"""


@pytest.fixture
def home(tmp_path):
    """A throwaway LLM_HOME with a minimal configuration."""
    (tmp_path / "config").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "config" / "llama-swap.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return tmp_path


MODULES = ("llmreg", "gpu_rocm", "gpu_vulkan")


@pytest.fixture
def load(home, monkeypatch):
    """Import llmreg fresh, with the given environment set BEFORE the import.

    A factory rather than a plain fixture returning the module, because llmreg and
    its two backend modules read LLM_HOME, LLM_ROCM_SMI, LLM_VULKANINFO,
    LLM_SYSFS_ROOT and LLM_BACKEND at IMPORT time. A monkeypatch inside the test
    body is therefore too late - three tests here were written that way and got
    the previous test's card list, which is a good illustration of why the bash
    harness runs a fresh interpreter per probe.

    SWAP_API points at port 9 (discard) so nothing can reach the machine's real
    llama-swap by accident. The bash suites had exactly that leak.
    """
    if LIB not in sys.path:
        sys.path.insert(0, LIB)

    def go(**env):
        monkeypatch.setenv("LLM_HOME", str(home))
        monkeypatch.setenv("LLM_SWAP_API", "http://127.0.0.1:9")
        monkeypatch.setenv("LLM_BACKEND", "rocm")
        for name in ("LLM_DGPUS", "LLM_MIN_VRAM_GB", "LLM_ROCM_SMI",
                     "LLM_VULKANINFO", "LLM_SYSFS_ROOT", "LLM_COMFY_GPU"):
            monkeypatch.delenv(name, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        for name in MODULES:
            sys.modules.pop(name, None)
        return importlib.import_module("llmreg")

    yield go
    for name in MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def reg(load):
    """llmreg with the defaults. Tests that fake hardware want `load` instead."""
    return load()


@pytest.fixture(scope="session")
def _fixture_dir(tmp_path_factory):
    """The rocm-smi and vulkaninfo fixtures, generated once per session.

    Into a temporary directory rather than over the committed copies, the same
    rule tests/lib.sh follows: regenerating in place would dirty the working tree
    on every run. tests/repo-matrix.sh is what keeps the committed ones current.
    """
    out = tmp_path_factory.mktemp("fixtures")
    env = dict(os.environ, LLM_FIXTURE_DIR=str(out))
    for gen in ("mk-smi.py", "mk-vulkan.py"):
        subprocess.run([sys.executable, os.path.join(REPO, "tests", "fixtures", gen)],
                       check=True, capture_output=True, env=env)
    return out


@pytest.fixture
def fixtures(_fixture_dir):
    """Path to a generated fixture, by file name."""
    def path(name: str, directory: bool = False) -> str:
        p = _fixture_dir / name
        assert p.is_dir() if directory else p.is_file(), "no such fixture: %s" % name
        return str(p)
    return path


@pytest.fixture
def add_block(home):
    """Append a model's marker block, the way tests/lib.sh add_block does.

    Only blocks with markers are visible to parse_config, so an entry written by
    hand without them is invisible - which is worth knowing when a test that
    should see a model does not.
    """
    def add(name: str, cmd: str, extra: str = "") -> None:
        cfg = home / "config" / "llama-swap.yaml"
        with open(cfg, "a", encoding="utf-8") as fh:
            fh.write('\n# >>> llm:%s\n  "%s":\n    cmd: "%s"\n    ttl: 900\n'
                     % (name, name, cmd))
            if extra:
                fh.write(extra + "\n")
            fh.write("# <<< llm:%s\n" % name)
    return add


#  ---------------------------------------------------------------------------
#  A real GGUF header, so the parser is tested against bytes and not a dict.
#  Only the types llmreg's reader supports; the layout is the upstream one:
#      magic, version, tensor count, kv count, then <key><type><value>...
#  ---------------------------------------------------------------------------
GGUF_UINT32, GGUF_UINT64, GGUF_STRING, GGUF_ARRAY = 4, 10, 8, 9


def _gstr(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def gguf_bytes(fields: list[tuple], tensor_count: int = 1) -> bytes:
    """(key, type, value) triples -> the bytes of a GGUF header."""
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", tensor_count)
    out += struct.pack("<Q", len(fields))
    for key, typ, val in fields:
        out += _gstr(key) + struct.pack("<I", typ)
        if typ == GGUF_STRING:
            out += _gstr(val)
        elif typ == GGUF_UINT32:
            out += struct.pack("<I", val)
        elif typ == GGUF_UINT64:
            out += struct.pack("<Q", val)
        elif typ == GGUF_ARRAY:
            elem_type, items = val
            out += struct.pack("<I", elem_type) + struct.pack("<Q", len(items))
            for item in items:
                out += struct.pack("<I", item) if elem_type == GGUF_UINT32 else _gstr(item)
        else:                                        # pragma: no cover
            raise AssertionError("unsupported type %d in the test writer" % typ)
    return out


LLAMA_FIELDS = [
    ("general.architecture", GGUF_STRING, "llama"),
    ("general.name", GGUF_STRING, "Fixture 7B"),
    ("llama.block_count", GGUF_UINT32, 32),
    ("llama.context_length", GGUF_UINT32, 32768),
    ("llama.attention.head_count", GGUF_UINT32, 32),
    ("llama.attention.head_count_kv", GGUF_UINT32, 8),
    ("llama.attention.key_length", GGUF_UINT32, 128),
    ("llama.attention.value_length", GGUF_UINT32, 128),
    #  Skipped by the reader, and it has to skip it without losing its place -
    #  which is the whole reason the reader parses values it does not want.
    ("tokenizer.ggml.tokens", GGUF_ARRAY, (GGUF_STRING, ["a", "b", "c"])),
    ("tokenizer.chat_template", GGUF_STRING, "{{ bos_token }}"),
]


@pytest.fixture
def gguf(home):
    """Write a GGUF file under models/ and return its path."""
    def make(name: str = "fixture", fields=None, truncate: int | None = None,
             padding: int = 4096) -> str:
        d = home / "models" / name
        d.mkdir(parents=True, exist_ok=True)
        blob = gguf_bytes(LLAMA_FIELDS if fields is None else fields)
        if truncate is not None:
            blob = blob[:truncate]
        else:
            blob += b"\0" * padding                  # stand-in for the tensor data
        path = d / ("%s.gguf" % name)
        path.write_bytes(blob)
        return str(path)
    return make
