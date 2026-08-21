#!/usr/bin/env bash
# ============================================================================
#  Check the registry's MCP surface against a throwaway LLM_HOME
# ============================================================================
#  Why: api-matrix covers the HTTP half and left the MCP half with no test at
#  all - the same catalog and the same write actions, through a different door.
#  That mattered the moment mcp 2.0 removed the module-wide get_context(): the
#  token now arrives through a Context injected into each tool, and nothing in
#  the suite would have noticed if a tool had lost its 'ctx' on the way and
#  started answering unauthenticated. So the gate is asserted from both sides.
#
#  Driven in-process through fastapi's TestClient, like api-matrix, with raw
#  JSON-RPC over /mcp. LLM_API_ALLOWED_HOSTS is set because TestClient sends
#  Host: testserver and the transport's DNS-rebinding protection answers 421
#  otherwise - which the checks below also pin down.
#
#  Needs venv-api (fastapi, mcp). Without it the checks are skipped, not failed.
#
#  Run with:  bash tests/mcp-matrix.sh      (or: bash tests/run-all.sh)
# ============================================================================
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

if ! have_api; then
  skip "the whole MCP suite" "venv-api is missing (llm setup)"
  summary
  exit $?
fi

H="$(sandbox)"
add_block "$H" small '${server} -m /home/x/models/small/small.gguf -c 8192'
printf 'testtoken\n' > "$H/config/api-token"

#  $1 = python body, `rpc` = one JSON-RPC call, `mod` = the loaded module.
#  LLM_SWAP_API points at a dead port: a write that gets past the token check
#  must not be able to reach a real llama-swap.
mcp(){ # $1=python body  [$2=extra env]
  LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
  LLM_SWAP_API="http://127.0.0.1:9" LLM_API_ALLOWED_HOSTS="${MCP_HOSTS:-testserver}" \
  LLM_API_REQUIRE_AUTH="${2:-}" \
    pyapi "
import importlib.util, json
spec = importlib.util.spec_from_file_location('llmapi', '$REPO/bin/llm-api.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from fastapi.testclient import TestClient

HDRS = {'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json',
        'MCP-Protocol-Version': '2025-06-18'}

def rpc(c, method, params=None, token=None, rid=1):
    h = dict(HDRS)
    if token:
        h['X-LLM-Token'] = token
    body = {'jsonrpc': '2.0', 'id': rid, 'method': method}
    if params is not None:
        body['params'] = params
    r = c.post('/mcp', json=body, headers=h)
    if r.status_code != 200:
        return {'httpStatus': r.status_code}
    #  The transport answers as one SSE event; the payload is the data: line.
    for line in r.text.splitlines():
        if line.startswith('data: '):
            return json.loads(line[6:])
    return {'noData': r.text[:200]}

def tool(c, name, args=None, token=None):
    return rpc(c, 'tools/call', {'name': name, 'arguments': args or {}},
               token=token, rid=9)['result']

with TestClient(mod.app) as c:
    $1"
}

section "the transport comes up"
check "initialize"           "llm-box" \
  "$(mcp "print(rpc(c, 'initialize', {'protocolVersion': '2025-06-18',
        'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}})
        ['result']['serverInfo']['name'])")"
check "every tool is offered" "9" \
  "$(mcp "print(len(rpc(c, 'tools/list', {})['result']['tools']))")"

#  The six the pi extension uses plus the three only MCP has - if one silently
#  disappears, an agent loses a capability with no error anywhere.
check "the tools are the documented ones" \
  "add_model,get_model,gpu_status,job_status,list_models,load_model,remove_model,set_model_config,unload_models" \
  "$(mcp "print(','.join(sorted(t['name'] for t in rpc(c, 'tools/list', {})['result']['tools'])))")"

section "the context carries the header (mcp 2.0)"
#  'ctx' is injected by type hint and must NOT show up as a tool argument -
#  a client would otherwise be asked to supply it.
check "ctx stays out of every schema" "" \
  "$(mcp "print(','.join(t['name'] for t in rpc(c, 'tools/list', {})['result']['tools']
        if 'ctx' in t['inputSchema'].get('properties', {})))")"

section "reads are open, writes need the token"
check "a read without a token"        "False" \
  "$(mcp "print(tool(c, 'list_models').get('isError', False))")"
check "a write without a token"       "True" \
  "$(mcp "print(tool(c, 'unload_models')['isError'])")"
check "and it says which header"      "True" \
  "$(mcp "print('X-LLM-Token' in tool(c, 'unload_models')['content'][0]['text'])")"
check "a write with the token"        "False" \
  "$(mcp "print(tool(c, 'unload_models', token='testtoken')['isError'])")"
#  Past the gate it reaches the network and fails there - LLM_SWAP_API is dead
#  on purpose. Reaching that error is the proof that the token was accepted.
check "which then reaches llama-swap" "True" \
  "$(mcp "print('refused' in tool(c, 'unload_models', token='testtoken')
        ['content'][0]['text'])")"
check "a wrong token is not enough"   "True" \
  "$(mcp "print(tool(c, 'unload_models', token='nope')['isError'])")"

section "LLM_API_REQUIRE_AUTH closes the reads too"
check "a read without a token"        "True" \
  "$(mcp "print(tool(c, 'list_models')['isError'])" 1)"
check "a read with the token"         "False" \
  "$(mcp "print(tool(c, 'list_models', token='testtoken')['isError'])" 1)"
check "a write is still closed"       "True" \
  "$(mcp "print(tool(c, 'unload_models')['isError'])" 1)"

section "the host check is not optional"
#  Without LLM_API_ALLOWED_HOSTS the transport rejects TestClient's own Host
#  header. That is the DNS-rebinding protection doing its job, and it is why
#  docs/API.md documents the variable for tailnet and alias hostnames.
check "an unknown Host is refused"    "421" \
  "$(MCP_HOSTS=example.invalid mcp "print(rpc(c, 'initialize', {'protocolVersion': '2025-06-18',
        'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}})['httpStatus'])")"

summary
