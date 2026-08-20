#!/usr/bin/env bash
# ============================================================================
#  Check the registry HTTP surface against a throwaway LLM_HOME
# ============================================================================
#  Why: bin/llm-api.py is what pi, Claude Code and any script actually talk to,
#  and it had no tests at all. It also carries the two shapes the catalog can
#  take - models and roles - and roles have no files, no card and no VRAM, so
#  every response builder needs a branch. Forgetting one turned GET
#  /api/models?slim=true into a 500.
#
#  Needs venv-api (fastapi, mcp). Without it the checks are skipped, not failed.
#
#  Run with:  bash tests/api-matrix.sh      (or: bash tests/run-all.sh)
# ============================================================================
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

if ! have_api; then
  skip "the whole registry suite" "venv-api is missing (llm setup)"
  summary
  exit $?
fi

#  One sandbox for the read-only checks: a card-0 model, an all-cards model, an
#  env-pinned whisper, an embedder, and a role over two of them.
H="$(sandbox)"
#  A sparse 40 GB file: weightsBytes comes from the file size, and the fit check
#  has nothing to weigh without one. Costs no disk.
mkdir -p "$H/models/big"
truncate -s 40G "$H/models/big/big.gguf"
add_block "$H" big \
  "\${server} -m $H/models/big/big.gguf -c 131072 -ctk q8_0 --device ROCm0 -sm none -mg 0"
add_block "$H" spread '${server} -m /home/x/models/spread/spread.gguf -c 65536'
add_block "$H" embed  '${server-embed} -m /home/x/models/embed/e.gguf -c 4096 --embedding'
add_block "$H" whisper '/bin/whisper-server -m /home/x/models/w/w.bin --request-path /v1/audio' \
  '    env:
      - "HIP_VISIBLE_DEVICES=1"'
LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" \
  pyx "llmreg.set_selector('mix', 'warm', ['big', 'spread'])" >/dev/null
printf 'testtoken\n' > "$H/config/api-token"

api(){ # $1=python body with `c` = TestClient, prints what you print
  LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_DGPUS= LLM_MIN_VRAM_GB= \
  LLM_SWAP_API="http://127.0.0.1:9" \
    pyapi "
import importlib.util, os
spec = importlib.util.spec_from_file_location('llmapi', '$REPO/bin/llm-api.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from fastapi.testclient import TestClient
c = TestClient(mod.app)
TOK = {'X-LLM-Token': 'testtoken'}
$1"
}

section "reading needs no token"
check "/api/health"            "200"  "$(api "print(c.get('/api/health').status_code)")"
check "health reports models"  "5"    "$(api "print(c.get('/api/health').json()['models'])")"
check "/api/models"            "200"  "$(api "print(c.get('/api/models').status_code)")"
check "/api/gpus"              "200"  "$(api "print(c.get('/api/gpus').status_code)")"
check "/api/state"             "200"  "$(api "print(c.get('/api/state').status_code)")"
check "/api/pi-models.json"    "200"  "$(api "print(c.get('/api/pi-models.json').status_code)")"
check "the self-describing index" "200" "$(api "print(c.get('/').status_code)")"
check "/api/jobs is empty"     "[]"   "$(api "print(c.get('/api/jobs').json())")"
check "an unknown model 404s"  "404"  "$(api "print(c.get('/api/models/ghost').status_code)")"

section "both catalog shapes come through"
check "models and roles listed" "big embed mix spread whisper" \
  "$(api "print(' '.join(sorted(m['id'] for m in c.get('/api/models').json())))")"
check "the role is tagged"      "role" \
  "$(api "print(next(m for m in c.get('/api/models').json() if m['id']=='mix')['kind'])")"
check "the role is reachable"   "200" "$(api "print(c.get('/api/models/mix').status_code)")"
check "role=chat includes roles" "big mix spread" \
  "$(api "print(' '.join(sorted(m['id'] for m in c.get('/api/models?role=chat').json())))")"
check "role=embed filters"      "embed" \
  "$(api "print(' '.join(m['id'] for m in c.get('/api/models?role=embed').json()))")"

#  The regression: _slim() read m['vram']['weightsBytes'] and m['runtime']
#  ['specDecoding'], neither of which a role has.
section "slim view survives roles"
check "slim answers at all"     "200" "$(api "print(c.get('/api/models?slim=true').status_code)")"
check "the role keeps its shape" "warm" \
  "$(api "print(next(m for m in c.get('/api/models?slim=true').json() if m['id']=='mix')['strategy'])")"
check "a model reports a size"  "True" \
  "$(api "print(next(m for m in c.get('/api/models?slim=true').json() if m['id']=='big')['sizeGB'] is not None)")"
check "a role reports no size"  "True" \
  "$(api "print('sizeGB' not in next(m for m in c.get('/api/models?slim=true').json() if m['id']=='mix'))")"
check "card 0 is not 'no card'" "card 0 only" \
  "$(api "print(next(m for m in c.get('/api/models?slim=true').json() if m['id']=='big')['gpu'])")"

section "slots are reported as the server will run them"
check "no -np flag means four"  "4" \
  "$(api "print(next(m for m in c.get('/api/models').json() if m['id']=='big')['runtime']['parallel'])")"
check "and a shared KV pool"    "True" \
  "$(api "print(next(m for m in c.get('/api/models').json() if m['id']=='big')['runtime']['kvUnified'])")"
check "whisper has neither"     "None" \
  "$(api "print(next(m for m in c.get('/api/models').json() if m['id']=='whisper')['runtime']['parallel'])")"

section "writing needs the token"
check "PATCH without token"     "401" \
  "$(api "print(c.patch('/api/models/big', json={'ttl': 60}).status_code)")"
check "PATCH with a wrong token" "401" \
  "$(api "print(c.patch('/api/models/big', json={'ttl': 60}, headers={'X-LLM-Token': 'no'}).status_code)")"
check "POST /api/unload without token" "401" \
  "$(api "print(c.post('/api/unload').status_code)")"
check "DELETE without token"    "401" \
  "$(api "print(c.delete('/api/models/big').status_code)")"

section "PATCH validation"
check "dryRun changes nothing"  "131072" \
  "$(api "
c.patch('/api/models/big?dryRun=true', json={'contextWindow': 4096, 'force': True}, headers=TOK)
print(c.get('/api/models/big').json()['runtime']['contextWindow'])")"
check "a bad slot count is refused" "400" \
  "$(api "print(c.patch('/api/models/big', json={'parallel': 0}, headers=TOK).status_code)")"
check "a bad card is refused"   "400" \
  "$(api "print(c.patch('/api/models/big', json={'gpu': 99}, headers=TOK).status_code)")"
check "whisper cannot span cards" "400" \
  "$(api "print(c.patch('/api/models/whisper', json={'gpu': 'both'}, headers=TOK).status_code)")"
#  40 GB of weights on a 34 GB card. The dryRun path reports the same refusal,
#  which is what a UI would show before offering to apply anything.
check "a model that does not fit 409s" "409" \
  "$(api "print(c.patch('/api/models/big', json={'gpu': 0}, headers=TOK).status_code)")"
check "and force overrides it"        "200" \
  "$(api "print(c.patch('/api/models/big?dryRun=true', json={'gpu': 0, 'force': True}, headers=TOK).status_code)")"
check "the refusal names the card"    "True" \
  "$(api "print('card 0' in c.patch('/api/models/big', json={'gpu': 0}, headers=TOK).json()['detail'])")"
check "adding needs a real repo" "400" \
  "$(api "print(c.post('/api/models', json={'repo': 'not a repo'}, headers=TOK).status_code)")"
check "extraFlags reject metacharacters" "400" \
  "$(api "print(c.post('/api/models', json={'repo': 'a/b', 'extraFlags': '; rm -rf /'}, headers=TOK).status_code)")"

section "a fresh checkout without a configuration"
check "GET /api/models says what is missing, not 500" "True" \
  "$(LLM_HOME="$(mktemp -d "$TMP/empty.XXXXXX")" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" \
     pyapi "
import importlib.util
spec = importlib.util.spec_from_file_location('llmapi', '$REPO/bin/llm-api.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
from fastapi.testclient import TestClient
r = TestClient(mod.app, raise_server_exceptions=False).get('/api/models')
print(r.status_code != 500)")"

summary
