"""The sidecar files that record where a model came from.

record_add, backfill, write_meta and file_digest had no test. They matter more
than their size suggests: .gitignore calls the .llm-model.json sidecars the only
thing in the tree that cannot simply be downloaded again, because once the Hugging
Face cache is cleaned the repo, revision and checksums are gone with it.
"""
import json
import os


def _cache(model_dir, sha=None, digests=None):
    """The parts of an `hf download` cache that llmreg reads back."""
    hf = os.path.join(model_dir, ".cache", "huggingface")
    if sha:
        dl = os.path.join(hf, "download")
        os.makedirs(dl, exist_ok=True)
        with open(os.path.join(dl, "model.gguf.metadata"), "w", encoding="utf-8") as fh:
            fh.write(sha + "\n")
    if digests:
        trees = os.path.join(hf, "trees")
        os.makedirs(trees, exist_ok=True)
        with open(os.path.join(trees, "%s.json" % (sha or "0" * 40)), "w",
                  encoding="utf-8") as fh:
            json.dump({"files": {k: {"lfs_sha256": v} for k, v in digests.items()}}, fh)


SHA = "a" * 40


def test_write_meta_merges_rather_than_replaces(reg, home):
    d = str(home / "models" / "m")
    reg.write_meta(d, {"repo": "unsloth/X-GGUF", "quant": "Q4_K_M"})
    reg.write_meta(d, {"revision": SHA})
    got = reg.read_meta(d)
    assert got == {"repo": "unsloth/X-GGUF", "quant": "Q4_K_M", "revision": SHA}


def test_write_meta_ignores_none_so_a_gap_cannot_erase_a_fact(reg, home):
    """A later call that does not know the repo must not delete the one on disk."""
    d = str(home / "models" / "m")
    reg.write_meta(d, {"repo": "unsloth/X-GGUF"})
    reg.write_meta(d, {"repo": None, "quant": "Q6_K"})
    assert reg.read_meta(d)["repo"] == "unsloth/X-GGUF"


def test_read_meta_of_a_missing_or_broken_sidecar(reg, home):
    d = home / "models" / "m"
    d.mkdir(parents=True)
    assert reg.read_meta(str(d)) is None
    (d / ".llm-model.json").write_text("{ this is not json", encoding="utf-8")
    #  None rather than a raise: a damaged sidecar means "provenance unknown",
    #  which every caller already handles, and 'llm ls' should still list models.
    assert reg.read_meta(str(d)) is None


def test_write_meta_is_atomic(reg, home):
    """Written to .tmp and renamed. Interrupted halfway, the old one is intact -
    and the sidecar is the file that cannot be regenerated."""
    d = str(home / "models" / "m")
    reg.write_meta(d, {"repo": "unsloth/X-GGUF"})
    assert not os.path.exists(reg.meta_path(d) + ".tmp")
    #  Read outside the assert: an expression with a side effect inside one
    #  vanishes under `python -O`, and the file handle has to be closed either
    #  way. CodeQL flagged both, correctly.
    with open(reg.meta_path(d), encoding="utf-8") as fh:
        written = json.load(fh)
    assert written["repo"] == "unsloth/X-GGUF"


def test_record_add_records_files_and_the_revision(reg, home, gguf):
    path = gguf("big")
    d = os.path.dirname(path)
    _cache(d, SHA, {"big.gguf": "deadbeef"})
    reg.record_add("big", "unsloth/Big-GGUF", "Q4_K_M")
    got = reg.read_meta(d)
    assert got["repo"] == "unsloth/Big-GGUF"
    assert got["quant"] == "Q4_K_M"
    assert got["revision"] == SHA
    assert got["source"] == "llm add"
    assert got["files"] == [{"name": "big.gguf",
                             "sizeBytes": os.path.getsize(path),
                             "sha256": "deadbeef"}]


def test_record_add_skips_the_cache_directory(reg, home, gguf):
    """.cache holds a copy of every blob under a hashed name. Listing those as
    the model's files would double every size and invent names nobody can map
    back to anything."""
    path = gguf("big")
    d = os.path.dirname(path)
    blobs = os.path.join(d, ".cache", "huggingface", "blobs")
    os.makedirs(blobs)
    with open(os.path.join(blobs, "abc123.gguf"), "wb") as fh:
        fh.write(b"\0" * 10)
    _cache(d, SHA)
    reg.record_add("big", "unsloth/Big-GGUF", "Q4_K_M")
    assert [f["name"] for f in reg.read_meta(d)["files"]] == ["big.gguf"]


def test_record_add_without_a_cache_still_records_what_it_knows(reg, home, gguf):
    """Provenance from the command line, revision unknown. Better than nothing:
    the repo and quant are what 'llm ls' shows and what a re-download needs."""
    path = gguf("big")
    reg.record_add("big", "unsloth/Big-GGUF", "Q4_K_M")
    got = reg.read_meta(os.path.dirname(path))
    assert got["repo"] == "unsloth/Big-GGUF"
    #  ABSENT, not None. write_meta drops None so a later call that does not know
    #  a fact cannot erase it, which means "unknown" reads as a missing key
    #  everywhere - worth pinning, because a caller testing `is None` would be
    #  wrong and a caller using .get() would be right.
    assert "revision" not in got


def test_the_digest_is_read_not_recomputed(reg, home, gguf, monkeypatch):
    """A 30 GB file is not hashed on every catalog read, so the number comes from
    the cache metadata. That makes it PROVENANCE and not an integrity check, which
    is what SECURITY.md says - a test here so the claim stays true."""
    path = gguf("big")
    d = os.path.dirname(path)
    _cache(d, SHA, {"big.gguf": "not-the-real-hash"})
    assert reg.file_digest(path) == "not-the-real-hash"


def test_no_digest_when_the_cache_says_nothing(reg, home, gguf):
    assert reg.file_digest(gguf("plain")) is None


def test_backfill_refuses_a_directory_that_is_not_there(reg):
    try:
        reg.backfill("nope")
    except KeyError:
        return
    raise AssertionError("backfill accepted a missing directory")


def test_backfill_with_an_explicit_repo_does_not_call_the_network(reg, home, gguf,
                                                                 monkeypatch):
    """verify=False is the offline path: trust what you were told, record it, and
    mark it unverified so the difference stays visible."""
    path = gguf("big")
    d = os.path.dirname(path)
    _cache(d, SHA)
    monkeypatch.setattr(reg, "hf_verify",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    out = reg.backfill("big", repo="unsloth/Big-GGUF", verify=False)
    assert out["repo"] == "unsloth/Big-GGUF"
    assert out["verified"] is False
    got = reg.read_meta(d)
    assert got["source"] == "manual"          # and not a German word, see repo-matrix
    assert got["verified"] is False


def test_backfill_marks_a_verified_repo_as_verified(reg, home, gguf, monkeypatch):
    path = gguf("big")
    d = os.path.dirname(path)
    _cache(d, SHA)
    monkeypatch.setattr(reg, "hf_verify", lambda repo, sha: "unsloth/Big-GGUF")
    out = reg.backfill("big", repo="unsloth/big-gguf")
    #  The canonical name from the API wins over what was typed.
    assert out["repo"] == "unsloth/Big-GGUF"
    assert out["verified"] is True
    assert reg.read_meta(d)["verified"] is True


def test_backfill_reads_the_quant_out_of_the_file_name(reg, home, gguf, monkeypatch):
    d = home / "models" / "q"
    d.mkdir(parents=True)
    (d / "Model-UD-Q4_K_XL.gguf").write_bytes(b"\0" * 16)
    _cache(str(d), SHA)
    monkeypatch.setattr(reg, "hf_verify", lambda repo, sha: repo)
    reg.backfill("q", repo="unsloth/Model-GGUF")
    assert reg.read_meta(str(d))["quant"] == "Q4_K_XL"


def test_backfill_candidates_strips_the_quant_and_guesses_publishers(reg):
    cands = reg.backfill_candidates("qwen3-8b-q4_k_m")
    assert "unsloth/qwen3-8b-GGUF" in cands
    assert not any("q4_k_m" in c.lower() for c in cands)


def test_backfill_candidates_knows_whisper_lives_in_collections(reg):
    """whisper.cpp models are .bin files in one big repo, not per-model GGUF
    repos, so the usual '<publisher>/<name>-GGUF' guess never matches."""
    cands = reg.backfill_candidates("whisper-large-v3-turbo")
    assert cands[0].endswith("whisper.cpp")


def test_backfill_candidates_are_unique_and_ordered(reg):
    cands = reg.backfill_candidates("qwen3-8b-q4_k_m")
    assert len(cands) == len(set(cands))
