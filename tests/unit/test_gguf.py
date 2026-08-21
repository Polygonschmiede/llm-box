"""Reading a model's geometry out of its own header.

gguf_meta and everything built on it - derive, vram_needed, kv_cache_bytes - were
reached only transitively through catalog(), so a malformed header had no test at
all. It is a realistic case: a download interrupted at 40 GB leaves a file that
opens fine and is not a GGUF.
"""
import struct

from conftest import GGUF_STRING, GGUF_UINT32, LLAMA_FIELDS, gguf_bytes


def test_reads_the_geometry(reg, gguf):
    meta = reg.gguf_meta(gguf())
    assert meta["general.architecture"] == "llama"
    assert meta["llama.block_count"] == 32
    assert meta["llama.attention.head_count_kv"] == 8


def test_skips_tokenizer_tables_without_losing_its_place(reg, gguf):
    """The fields AFTER a skipped array still parse.

    tokenizer.* is dropped, but its bytes still have to be consumed - the reader
    parses values it does not want for exactly this reason. If it seeked wrongly,
    the key after the token array would come out as garbage rather than missing,
    which is the failure that looks like a corrupt file.
    """
    meta = reg.gguf_meta(gguf())
    assert "tokenizer.ggml.tokens" not in meta
    assert meta["tokenizer.chat_template"] == "{{ bos_token }}"


def test_arch_get_follows_the_architecture(reg, gguf):
    meta = reg.gguf_meta(gguf())
    assert reg._arch_get(meta, "block_count") == 32
    assert reg._arch_get(meta, "nonsense", "fallback") == "fallback"
    #  An unknown architecture must not raise; the key simply is not there.
    assert reg._arch_get({"general.architecture": "mystery"}, "block_count") is None


def test_a_truncated_header_is_no_metadata(reg, gguf):
    """An interrupted download, which is the common way this file goes wrong."""
    assert reg.gguf_meta(gguf("cut", truncate=40)) == {}


def test_a_file_that_is_not_gguf_at_all(reg, home):
    d = home / "models" / "html"
    d.mkdir(parents=True)
    #  What an unauthenticated download of a gated repo actually leaves behind.
    (d / "html.gguf").write_bytes(b"<!DOCTYPE html>\n<html><body>404</body></html>\n")
    assert reg.gguf_meta(str(d / "html.gguf")) == {}


def test_a_missing_file_is_no_metadata(reg, home):
    assert reg.gguf_meta(str(home / "models" / "nope" / "nope.gguf")) == {}


def test_a_lying_kv_count_does_not_hang(reg, home):
    """A header claiming more fields than it has.

    The reader caps at 2000 and stops on the first short read. Without both it
    would either loop or raise, and this file is attacker-influenceable: it is
    whatever the model repository served.
    """
    d = home / "models" / "liar"
    d.mkdir(parents=True)
    blob = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1) + struct.pack("<Q", 10**9)
    blob += gguf_bytes([("general.architecture", GGUF_STRING, "llama")])[24:]
    (d / "liar.gguf").write_bytes(blob)
    assert reg.gguf_meta(str(d / "liar.gguf")) == {}


def test_vram_needed_adds_the_headroom(reg):
    assert reg.vram_needed(1000, 0) == int(1000 * reg.VRAM_HEADROOM)
    assert reg.vram_needed(1000, 500) == int(1500 * reg.VRAM_HEADROOM)


def test_vram_needed_without_weights_is_unknown(reg):
    """None, not zero. A model whose size is unknown must not read as 'fits'."""
    assert reg.vram_needed(None, 500) is None
    assert reg.vram_needed(0, 500) is None


def test_kv_cache_from_a_real_header(reg, gguf):
    """The same arithmetic the bash suite checks, but from bytes on disk.

    32 layers * 8 kv heads * (128 + 128) * 8192 tokens * 2 bytes.
    """
    path = gguf()
    assert reg.kv_cache_bytes(path, 8192, "f16", 1) == 32 * 8 * 256 * 8192 * 2


def test_kv_cache_of_a_broken_header_is_unknown(reg, gguf):
    assert reg.kv_cache_bytes(gguf("cut2", truncate=40), 8192, "f16", 1) is None


def test_the_header_is_read_once(reg, gguf, monkeypatch):
    """The cache is not a nicety: catalog() asks for every model on every call."""
    path = gguf()
    reg.gguf_meta(path)
    opened = []
    real_open = open

    def spy(*a, **k):
        opened.append(a[0])
        return real_open(*a, **k)

    monkeypatch.setattr("builtins.open", spy)
    reg.gguf_meta(path)
    assert path not in opened


def test_context_length_comes_from_the_header(reg, gguf):
    meta = reg.gguf_meta(gguf())
    assert reg._arch_get(meta, "context_length") == 32768
    #  A model with no stated maximum: callers fall back rather than assume one.
    other = gguf("nolimit", fields=[f for f in LLAMA_FIELDS
                                    if f[0] != "llama.context_length"])
    assert reg._arch_get(reg.gguf_meta(other), "context_length") is None


def test_an_unsupported_value_type_is_no_metadata(reg, home):
    """Unknown type -> the whole header is discarded rather than half-read."""
    d = home / "models" / "weird"
    d.mkdir(parents=True)
    blob = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1) + struct.pack("<Q", 1)
    blob += struct.pack("<Q", 4) + b"key1" + struct.pack("<I", 99)   # type 99
    (d / "weird.gguf").write_bytes(blob + b"\0" * 32)
    assert reg.gguf_meta(str(d / "weird.gguf")) == {}


def test_uint32_and_string_round_trip(reg, gguf):
    """The writer in conftest has to agree with the reader, or nothing here means
    anything. Asserted explicitly so a bug in the test helper shows up as a
    failure of the helper rather than of the code."""
    path = gguf("rt", fields=[("general.architecture", GGUF_STRING, "qwen3"),
                              ("qwen3.block_count", GGUF_UINT32, 7)])
    meta = reg.gguf_meta(path)
    assert meta == {"general.architecture": "qwen3", "qwen3.block_count": 7}
