"""What happens when the machine does not cooperate.

The bash suites have nine error-path checks between them, all about a refused
value. These are about a refused ENVIRONMENT: a config that will not parse, a
directory that cannot be written, a lock somebody else holds. None of it was
covered, and all of it is what a fresh or damaged installation looks like.
"""
import os
import stat
import threading
import time

import pytest


def test_no_configuration_names_the_command_that_fixes_it(load, tmp_path):
    """Not a bare FileNotFoundError: this is the state of every clone before
    'llm init', and it used to travel out as an HTTP 500 with a traceback."""
    empty = tmp_path / "empty"
    (empty / "config").mkdir(parents=True)
    reg = load(LLM_HOME=str(empty))
    with pytest.raises(reg.ConfigMissing) as caught:
        reg.config_text()
    assert "llm init" in str(caught.value)


def test_config_missing_is_a_filenotfounderror(load, tmp_path):
    """So a caller that only knows the standard exceptions still catches it."""
    empty = tmp_path / "empty2"
    (empty / "config").mkdir(parents=True)
    reg = load(LLM_HOME=str(empty))
    with pytest.raises(FileNotFoundError):
        reg.parse_config()


def test_a_config_without_a_models_section_is_refused_with_the_reason(reg, home):
    """put_block CREATES a missing marker block, and it needs 'models:' to know
    where. Returning the text unchanged is what it used to do, and all four
    callers then reported success while writing nothing."""
    (home / "config" / "llama-swap.yaml").write_text("logLevel: info\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        reg.sync_groups()
    assert "models:" in str(caught.value)


def test_a_config_that_is_not_yaml_at_all_still_parses_to_nothing(reg, home):
    """The reader is line-based on purpose - llama-swap's own format carries
    command lines that a YAML parser reflows - so garbage yields no models rather
    than an exception. Worth pinning: 'no models' is a survivable answer, a
    traceback out of catalog() is not."""
    (home / "config" / "llama-swap.yaml").write_text(
        "\x00\x01 not yaml \xff\nmodels:\n", encoding="utf-8", errors="replace")
    assert reg.parse_config() == []


def test_an_unreadable_configuration_is_not_silently_empty(reg, home):
    """A permissions mistake has to be loud. Reporting 'no models' would look
    like an empty installation and invite an 'llm init' over the top of it."""
    cfg = home / "config" / "llama-swap.yaml"
    os.chmod(cfg, 0)
    try:
        with pytest.raises(OSError):
            reg.config_text()
    finally:
        os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)


def test_a_read_only_home_refuses_a_write_rather_than_losing_it(reg, home, add_block):
    """The failure mode this prevents: _write_config renames a temp file over the
    real one, so a half-written config is impossible - but the directory has to be
    writable for the temp file at all, and that has to be an error, not a no-op."""
    add_block("big", "${server} -m /m/big.gguf -c 4096")
    before = reg.config_text()
    os.chmod(home / "config", stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(OSError):
            reg._write_config(before + "\n# appended\n")
    finally:
        os.chmod(home / "config", stat.S_IRWXU)
    assert reg.config_text() == before


def test_the_config_lock_serialises_writers(reg):
    """Two 'llm add' runs at once must not interleave. The lock is flock on a
    separate file, so this asserts that the second holder waits."""
    order = []

    def hold():
        with reg.config_lock():
            order.append("first-in")
            time.sleep(0.3)
            order.append("first-out")

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.1)
    with reg.config_lock():
        order.append("second-in")
    t.join(timeout=5)
    assert order == ["first-in", "first-out", "second-in"]


def test_check_fit_without_a_known_weight_does_not_refuse(load, fixtures):
    """weightsBytes None means the GGUF header could not be read. Refusing then
    would block every model with a damaged sidecar; the fit is simply unknown."""
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    model = {"runtime": {"gpu": {"device": 0, "mode": "single"}, "contextWindow": 4096,
                         "kvCacheQuant": None, "parallel": 1, "specDecoding": None},
             "vram": {"weightsBytes": None},
             "files": {"model": {"path": "/nope.gguf"}},
             "name": "x", "state": "stopped"}
    out = reg.check_fit(model)
    assert out["ok"] is True
    assert out["needBytes"] == 0


def test_check_fit_names_the_tight_card_and_not_the_total(load, fixtures):
    """The bug this arithmetic had: summing free VRAM across cards and reporting
    'fits' when one card alone could not hold its share."""
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    model = {"runtime": {"gpu": {"device": None, "mode": "both"}, "contextWindow": 4096,
                         "kvCacheQuant": None, "parallel": 1, "specDecoding": None},
             "vram": {"weightsBytes": 60 * 1024**3},
             "files": {"model": {"path": "/nope.gguf"}},
             "name": "x", "state": "stopped"}
    out = reg.check_fit(model)
    assert out["ok"] is False
    assert "per card" in out["reason"]


def test_patch_model_of_an_unknown_name(reg, add_block):
    add_block("big", "${server} -m /m/big.gguf -c 4096")
    with pytest.raises(KeyError):
        reg.patch_model("nope", {"ttl": 60})


def test_patch_model_dry_run_touches_nothing(reg, add_block):
    add_block("big", "${server} -m /m/big.gguf -c 4096")
    before = reg.config_text()
    reg.patch_model("big", {"ttl": 60}, dry_run=True)
    assert reg.config_text() == before


def test_remove_model_refuses_to_delete_outside_models(reg, home, add_block):
    """The containment check, exercised through the path that reaches it.

    'llm rm --files' deletes the directory of the model's -m file, and that path
    comes out of the configuration - which a person edits. So a model pointing
    somewhere else entirely has to be removed from the config and have its
    directory LEFT ALONE. This used to be an rm -rf with the name interpolated
    into a sed address and no containment check at all.

    Written against a real directory outside models/ rather than by patching a
    helper: the first version of this test monkeypatched a function that does not
    exist, so it asserted that an untouched directory was untouched.
    """
    outside = home / "elsewhere"
    outside.mkdir()
    weights = outside / "escaped.gguf"
    weights.write_bytes(b"\0" * 32)
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    add_block("esc", "${server} -m %s -c 4096" % weights)
    out = reg.remove_model("esc", delete_files=True)
    assert outside.is_dir()
    assert (outside / "keep.txt").exists()
    assert out.get("removed") in (None, [], ())
    #  And it IS gone from the configuration - refusing the deletion must not
    #  refuse the removal.
    assert "esc" not in [e["name"] for e in reg.parse_config()]


def test_a_model_name_with_regex_characters_is_escaped(reg, add_block):
    """The name becomes part of a marker pattern. Unescaped, 'a.b' would match
    'axb' and remove the wrong block - or, worse, half of two."""
    add_block("a.b", "${server} -m /m/one.gguf -c 4096")
    add_block("axb", "${server} -m /m/two.gguf -c 4096")
    reg.remove_model("a.b")
    left = [e["name"] for e in reg.parse_config()]
    assert left == ["axb"]
