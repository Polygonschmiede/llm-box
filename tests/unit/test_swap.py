"""Talking to llama-swap: loading, unloading, restarting.

These three had no test that could see a success. LLM_SWAP_API pointed at a port
nothing listens on, so every bash check exercised the failure path only - which
means the code that runs when llama-swap ANSWERS was never executed anywhere.
Here it answers, from a throwaway HTTP server on a loopback port.
"""
import http.server
import json
import threading

import pytest


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):                       # keep the test output clean
        pass

    def _reply(self):
        route = self.server.routes.get(self.path)
        self.server.seen.append((self.command, self.path))
        if route is None:
            self.send_error(404)
            return
        status, body = route
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _reply

    def do_POST(self):
        #  Read the body before replying, or the client sees a closed connection.
        length = int(self.headers.get("Content-Length") or 0)
        self.server.bodies.append(self.rfile.read(length) if length else b"")
        self._reply()


@pytest.fixture
def swap():
    """A stand-in llama-swap. `routes` maps path -> (status, json body)."""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    srv.routes, srv.seen, srv.bodies = {}, [], []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.url = "http://127.0.0.1:%d" % srv.server_address[1]
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _model(load, swap, gguf, add_block, **env):
    path = gguf("big")
    add_block("big", "${server} -m %s -c 4096" % path)
    return load(LLM_SWAP_API=swap.url, **env)


def test_load_model_sends_a_one_token_request(load, swap, gguf, add_block, fixtures):
    """The cheapest thing that makes llama-swap start a model: max_tokens 1."""
    swap.routes["/v1/chat/completions"] = (200, {"choices": []})
    reg = _model(load, swap, gguf, add_block, LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    out = reg.load_model("big")
    assert out["model"] == "big"
    assert out["state"] == "ready"
    assert isinstance(out["seconds"], float)
    assert ("POST", "/v1/chat/completions") in swap.seen
    body = json.loads(swap.bodies[-1])
    assert body["max_tokens"] == 1
    assert body["model"] == "big"


def test_load_model_of_an_unknown_name(load, swap, gguf, add_block, fixtures):
    reg = _model(load, swap, gguf, add_block, LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    with pytest.raises(KeyError):
        reg.load_model("nope")


def test_an_embedding_model_is_loaded_through_its_own_endpoint(load, swap, gguf,
                                                              add_block, fixtures):
    """Sending a chat request to an embedding server is a 400, not a load - the
    role decides the endpoint."""
    swap.routes["/v1/embeddings"] = (200, {"data": []})
    path = gguf("emb")
    add_block("emb", "${server} -m %s -c 4096 --embedding" % path)
    reg = load(LLM_SWAP_API=swap.url, LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    reg.load_model("emb")
    assert ("POST", "/v1/embeddings") in swap.seen


def test_whisper_is_not_loadable_on_request(load, swap, gguf, add_block, fixtures):
    """It only starts on real audio, so asking is refused with the reason rather
    than by timing out against an endpoint that will never answer."""
    add_block("w", "/bin/whisper-server -m /m/w.bin --request-path /v1/audio")
    reg = load(LLM_SWAP_API=swap.url, LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    with pytest.raises(ValueError):
        reg.load_model("w")
    #  Not "nothing was requested" - get_model() asks llama-swap what is loaded
    #  first. What matters is that no attempt was made to LOAD it.
    assert not [p for _verb, p in swap.seen if p.startswith("/v1/audio")]


def test_unload_all_reports_success(load, swap):
    swap.routes["/unload"] = (200, None)
    reg = load(LLM_SWAP_API=swap.url)
    assert reg.unload_all() == {"unloaded": True}
    assert ("GET", "/unload") in swap.seen


def test_unload_all_reports_the_reason_instead_of_raising(load):
    """The caller wants to show why, and an exception out of here would reach the
    control page as a 500 for something that is only 'the service is down'."""
    reg = load(LLM_SWAP_API="http://127.0.0.1:9")
    out = reg.unload_all()
    assert out["unloaded"] is False
    assert out["error"]


def test_reload_swap_restarts_only_a_running_service(reg, monkeypatch):
    """If it is not running there is nothing to reload, and starting it would be a
    different decision than the caller made."""
    calls = []

    class Result:
        returncode = 1                              # is-active says no

    monkeypatch.setattr(reg.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), Result())[1])
    assert reg.reload_swap() is False
    assert len(calls) == 1
    assert "is-active" in calls[0]


def test_reload_swap_restarts_when_it_is_running(reg, monkeypatch):
    calls = []

    class Ok:
        returncode = 0

    monkeypatch.setattr(reg.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), Ok())[1])
    assert reg.reload_swap() is True
    assert any("restart" in c for c in calls)


def test_reload_swap_without_systemctl_is_false_not_a_crash(reg, monkeypatch):
    def boom(*a, **k):
        raise OSError("no systemctl")

    monkeypatch.setattr(reg.subprocess, "run", boom)
    assert reg.reload_swap() is False


def test_live_against_a_server_that_answers(load, swap):
    """The success path of live(), which nothing exercised: every bash check ran
    it against a refused connection."""
    swap.routes["/v1/models"] = (200, {"data": [{"id": "big",
                                                 "status": {"value": "ready"}}]})
    swap.routes["/running"] = (200, {"running": [{"model": "big"}]})
    reg = load(LLM_SWAP_API=swap.url)
    st = reg.live()
    assert st["up"] is True
    assert st["states"] == {"big": "ready"}
    assert st["running"] == [{"model": "big"}]


def test_live_reports_up_even_when_running_fails(load, swap):
    """Two calls, and the second one is optional. An older llama-swap without
    /running still counts as up, with an empty list - reporting it as DOWN would
    hide every loaded model."""
    swap.routes["/v1/models"] = (200, {"data": []})
    reg = load(LLM_SWAP_API=swap.url)
    st = reg.live()
    assert st["up"] is True
    assert st["running"] == []


def test_live_reports_a_state_it_does_not_recognise_as_unknown(load, swap):
    swap.routes["/v1/models"] = (200, {"data": [{"id": "big"}]})
    reg = load(LLM_SWAP_API=swap.url)
    assert reg.live()["states"] == {"big": "unknown"}


def test_live_against_a_server_that_is_not_there(load):
    reg = load(LLM_SWAP_API="http://127.0.0.1:9")
    st = reg.live()
    assert st["up"] is False
    assert st["running"] == []
