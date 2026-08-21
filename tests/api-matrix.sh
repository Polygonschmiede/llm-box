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
LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
  pyx "
llmreg.set_selector('mix', 'warm', ['big', 'spread'])
#  the group block is generated, not written by hand - the page reads it
llmreg._write_config(llmreg.sync_groups())" >/dev/null
printf 'testtoken\n' > "$H/config/api-token"

api(){ # $1=python body with `c` = TestClient, prints what you print
  LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
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

section "the endpoints the control page needs"
check "/api/versions"          "200" "$(api "print(c.get('/api/versions').status_code)")"
check "it names llm-box"       "True" \
  "$(api "print('llmBox' in c.get('/api/versions').json())")"
check "/api/config"            "200" "$(api "print(c.get('/api/config').status_code)")"
check "macros come through"    "True" \
  "$(api "print('server' in c.get('/api/config').json()['macros'])")"
check "groups carry persistent" "True" \
  "$(api "
g = c.get('/api/config').json()['groups']
print(all('persistent' in v for v in g.values()) if g else 'no groups')")"
check "/api/config/diff"       "200" "$(api "print(c.get('/api/config/diff').status_code)")"
check "/api/roles"             "200" "$(api "print(c.get('/api/roles').status_code)")"
check "the role is listed"     "mix"  \
  "$(api "print(' '.join(c.get('/api/roles').json()))")"
check "/ui is served"          "200" "$(api "print(c.get('/ui').status_code)")"
check "/ui is html"            "True" \
  "$(api "print('text/html' in c.get('/ui').headers['content-type'])")"
check "the index points at it" "/ui" \
  "$(api "print(c.get('/').json()['ui'])")"
#  The page loads the design system from this server, so those two files are
#  part of the endpoint surface now. A table of names, not a path under the
#  document root: this process can read every model file and config/api-token.
check "/ui/stellar.css"        "200" \
  "$(api "print(c.get('/ui/stellar.css').status_code)")"
check "it is served as CSS"    "True" \
  "$(api "print('text/css' in c.get('/ui/stellar.css').headers['content-type'])")"
check "and it is the real file" "True" \
  "$(api "print('--paper-0' in c.get('/ui/stellar.css').text)")"
check "the dark overlay too"   "200" \
  "$(api "print(c.get('/ui/stellar-auto-dark.css').status_code)")"
check "an unknown asset"       "404" \
  "$(api "print(c.get('/ui/nonsense.css').status_code)")"
check "and no way out of the table" "404" \
  "$(api "print(c.get('/ui/..%2f..%2fconfig%2fapi-token').status_code)")"
check "the index lists the route" "True" \
  "$(api "print(any('/ui/' in r for r in c.get('/').json()['read']))")"
#  78 KB of design system on every page load, or not. FileResponse sends an
#  ETag but answers no conditional request by itself, so this is the check that
#  the four lines doing it by hand still work.
check "an unchanged sheet is a 304" "304" \
  "$(api "
tag = c.get('/ui/stellar.css').headers['etag']
print(c.get('/ui/stellar.css', headers={'If-None-Match': tag}).status_code)")"
check "and a stale one is not"     "200" \
  "$(api "print(c.get('/ui/stellar.css', headers={'If-None-Match': '\"old\"'}).status_code)")"

#  Roles were CLI-only until now, which meant a UI could show them and not
#  change them.
section "roles over HTTP"
check "PUT needs the token"    "401" \
  "$(api "print(c.put('/api/roles/r', json={'strategy':'pin','targets':['big']}).status_code)")"
check "DELETE needs the token" "401" "$(api "print(c.delete('/api/roles/mix').status_code)")"
check "an unknown strategy"    "400" \
  "$(api "print(c.put('/api/roles/r', json={'strategy':'bogus','targets':['big']}, headers=TOK).status_code)")"
check "targets must be a list" "400" \
  "$(api "print(c.put('/api/roles/r', json={'strategy':'pin','targets':'big'}, headers=TOK).status_code)")"
check "an unknown target"      "400" \
  "$(api "print(c.put('/api/roles/r', json={'strategy':'pin','targets':['ghost']}, headers=TOK).status_code)")"
check "a bad spillover count"  "400" \
  "$(api "print(c.put('/api/roles/r', json={'strategy':'spillover','targets':['big','spread'],'spillover':0}, headers=TOK).status_code)")"
check "a name with a space"    "400" \
  "$(api "print(c.put('/api/roles/bad name', json={'strategy':'pin','targets':['big']}, headers=TOK).status_code)")"
check "a name colliding with a model" "400" \
  "$(api "print(c.put('/api/roles/big', json={'strategy':'pin','targets':['big']}, headers=TOK).status_code)")"
check "dryRun writes nothing"  "False" \
  "$(api "
c.put('/api/roles/probe?dryRun=true', json={'strategy':'pin','targets':['big']}, headers=TOK)
print('probe' in c.get('/api/roles').json())")"
check "a real PUT persists"    "True" \
  "$(api "
c.put('/api/roles/probe', json={'strategy':'pin','targets':['big']}, headers=TOK)
print('probe' in c.get('/api/roles').json())")"
check "and appears as a model" "role" \
  "$(api "
c.put('/api/roles/probe', json={'strategy':'pin','targets':['big']}, headers=TOK)
print(c.get('/api/models/probe').json()['kind'])")"
check "DELETE removes it"      "False" \
  "$(api "
c.put('/api/roles/probe', json={'strategy':'pin','targets':['big']}, headers=TOK)
c.delete('/api/roles/probe', headers=TOK)
print('probe' in c.get('/api/roles').json())")"
check "DELETE of an unknown role" "404" \
  "$(api "print(c.delete('/api/roles/ghost', headers=TOK).status_code)")"

#  The page exchanges the token for a cookie once, so it never keeps a secret
#  in a form field.
section "sessions"
check "no session, no write"   "False" \
  "$(api "print(c.get('/api/session').json()['canWrite'])")"
check "a wrong token"          "401" \
  "$(api "print(c.post('/api/session', json={'token':'nope'}).status_code)")"
check "the right token"        "200" \
  "$(api "print(c.post('/api/session', json={'token':'testtoken'}).status_code)")"
check "the cookie grants write" "True" \
  "$(api "
c.post('/api/session', json={'token':'testtoken'})
print(c.get('/api/session').json()['canWrite'])")"
check "and a write goes through" "200" \
  "$(api "
c.post('/api/session', json={'token':'testtoken'})
print(c.patch('/api/models/spread?dryRun=true', json={'ttl': 60}).status_code)")"
check "signing out revokes it" "401" \
  "$(api "
c.post('/api/session', json={'token':'testtoken'})
c.delete('/api/session')
print(c.patch('/api/models/big', json={'ttl': 60}).status_code)")"
check "reads stay open by default" "False" \
  "$(api "print(c.get('/api/session').json()['readNeedsAuth'])")"

#  pi-models.json is the one read that stays open so a client can bootstrap -
#  but once an inference key is set the payload carries it, and handing that to
#  anyone who reaches the port would undo the point of setting it.
section "pi-models.json follows its content"
check "open while no key is set"     "200" \
  "$(api "print(c.get('/api/pi-models.json').status_code)")"
check "it carries the placeholder"   "sk-local" \
  "$(api "print(c.get('/api/pi-models.json').json()['providers']['llm-box']['apiKey'])")"
check "gated once a key exists"      "401" \
  "$(api "
llmreg = __import__('llmreg')
llmreg.api_key(create=True)
print(c.get('/api/pi-models.json').status_code)")"
check "and readable with the token"  "True" \
  "$(api "
llmreg = __import__('llmreg')
k = llmreg.api_key(create=True)
r = c.get('/api/pi-models.json', headers=TOK)
print(r.status_code == 200 and r.json()['providers']['llm-box']['apiKey'] == k)")"
check "health stays open regardless" "200" \
  "$(api "
llmreg = __import__('llmreg')
llmreg.api_key(create=True)
print(c.get('/api/health').status_code)")"

#  Updating used to be CLI-only, so the control page could show a stale version
#  and do nothing about it. These checks never let a real update start: the
#  refusals all happen before job_start, and the 'busy' case injects a job
#  instead of running one.
section "updates over HTTP"
check "an update needs the token"   "401" \
  "$(api "print(c.post('/api/updates/llama').status_code)")"
check "a rollback needs the token"  "401" \
  "$(api "print(c.post('/api/rollback/llama').status_code)")"
check "an unknown component"        "400" \
  "$(api "print(c.post('/api/updates/nonsense', headers=TOK).status_code)")"
check "the refusal lists the real ones" "True" \
  "$(api "print('whisper' in c.post('/api/updates/nonsense', headers=TOK).json()['detail'])")"
check "'all' is an update, not a rollback" "400" \
  "$(api "print(c.post('/api/rollback/all', headers=TOK).status_code)")"
check "a version with metacharacters" "400" \
  "$(api "print(c.post('/api/updates/llama', json={'version': '; reboot'}, headers=TOK).status_code)")"
check "one at a time"               "409" \
  "$(api "
mod.JOBS['fake'] = {'id': 'fake', 'kind': 'update', 'state': 'running',
                    'argv': ['llm', 'update', 'llama'], 'log': []}
print(c.post('/api/updates/whisper', headers=TOK).status_code)")"
check "and it says which one"       "True" \
  "$(api "
mod.JOBS['fake'] = {'id': 'fake', 'kind': 'update', 'state': 'running',
                    'argv': ['llm', 'update', 'llama'], 'log': []}
print('update llama' in c.post('/api/updates/whisper', headers=TOK).json()['detail'])")"
check "the index lists them"        "True" \
  "$(api "print(any('updates' in w for w in c.get('/').json()['write']))")"
#  The job log ends up in a web page, where an escape sequence would be printed
#  rather than interpreted.
check "colour is stripped from a job log" "build ok" \
  "$(api "print(mod.ANSI.sub('', '\033[0;36mbuild\033[0m \033[0;32mok\033[0m'))")"

summary
