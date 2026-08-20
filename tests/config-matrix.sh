#!/usr/bin/env bash
# ============================================================================
#  Check the configuration read/write path against a throwaway LLM_HOME
# ============================================================================
#  Why: everything the CLI, the registry and the pi extension believe about a
#  model is derived from the marker blocks in llama-swap.yaml by hand-written
#  regex, and the blocks are rewritten in place. Two failures found without any
#  test in place - sync_groups() silently doing nothing when its marker was
#  missing, and derive() looping forever on a path outside models/ - both lived
#  here. This file is the net under that.
#
#  Run with:  bash tests/config-matrix.sh      (or: bash tests/run-all.sh)
# ============================================================================
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

SMI2="$FIXTURES/rocm-smi-2card.sh"
SMI_IGPU="$FIXTURES/rocm-smi-igpu-first.sh"

#  Every probe gets its own sandbox: these functions write, and a shared one
#  would make the order of the checks matter.
cfg(){ # -> LLM_HOME with one card-0 model, one env-pinned whisper, one all-cards
  local home; home="$(sandbox)"
  add_block "$home" big \
    '${server} -m /home/x/models/big/big.gguf -c 131072 -ctk q8_0 -ctv q8_0 --device ROCm0 -sm none -mg 0'
  add_block "$home" spread \
    '${server} -m /home/x/models/spread/spread.gguf -c 65536'
  add_block "$home" whisper \
    '/bin/whisper-server -m /home/x/models/whisper/w.bin --request-path /v1/audio' \
    '    env:
      - "HIP_VISIBLE_DEVICES=1"'
  printf '%s' "$home"
}

run(){ # $1=LLM_HOME  $2=python -> stdout
  LLM_HOME="$1" LLM_ROCM_SMI="$SMI2" LLM_DGPUS= LLM_MIN_VRAM_GB= \
  LLM_SWAP_API="http://127.0.0.1:9" pyx "$2"
}

# ---------------------------------------------------------------------------
section "parse_config sees only marker blocks"
H="$(cfg)"
check "three blocks found"    "big spread whisper" \
  "$(run "$H" "print(' '.join(e['name'] for e in llmreg.parse_config()))")"
check "cmd of one block"      "131072" \
  "$(run "$H" "print(llmreg.flag([e for e in llmreg.parse_config() if e['name']=='big'][0]['cmd'], '-c'))")"
check "find_block hits"       "True" \
  "$(run "$H" "print(bool(llmreg.find_block('big', llmreg.config_text())))")"
check "find_block misses"     "False" \
  "$(run "$H" "print(bool(llmreg.find_block('nope', llmreg.config_text())))")"
#  A hand-written entry without markers is invisible - worth pinning down,
#  because 'llm edit' lets anyone create exactly that.
H2="$(cfg)"; printf '  "bare":\n    cmd: "x"\n' >> "$H2/config/llama-swap.yaml"
check "unmarked entry ignored" "3" \
  "$(run "$H2" "print(len(llmreg.parse_config()))")"

# ---------------------------------------------------------------------------
#  The flag primitives. del_flag() removes the flag AND the token after it by
#  default, which is right for '-c 8192' and wrong for a bare switch - and
#  set_flag(name, None) handed it the default anyway. Testing the caller is not
#  enough here: _patch_model deletes the switch itself before re-setting it, so
#  it masks the bug. These check the primitive.
section "set_flag / del_flag on bare switches"
FL="\${server} -m /a.gguf -c 8192 -kvu -ctk q8_0 --device ROCm0"
check "setting a switch keeps the next flag" "q8_0" \
  "$(run "$H" "print(llmreg.flag(llmreg.set_flag('$FL', '-kvu', None), '-ctk'))")"
check "setting a switch does not duplicate it" "1" \
  "$(run "$H" "print(llmreg.set_flag('$FL', '-kvu', None).count('-kvu'))")"
check "deleting a switch keeps the next flag" "q8_0" \
  "$(run "$H" "print(llmreg.flag(llmreg.del_flag('$FL', '-kvu', with_value=False), '-ctk'))")"
check "a valued flag replaces cleanly" "4096" \
  "$(run "$H" "print(llmreg.flag(llmreg.set_flag('$FL', '-c', 4096), '-c'))")"
check "a valued flag is not duplicated" "1" \
  "$(run "$H" "print(llmreg.set_flag('$FL', '-c', 4096).count('-c '))")"
check "switch at the end of the line" "q8_0" \
  "$(run "$H" "print(llmreg.flag(llmreg.set_flag('\${server} -ctk q8_0 -kvu', '-kvu', None), '-ctk'))")"

# ---------------------------------------------------------------------------
section "gpu_of: both ways of pinning, and card 0 is not 'no card'"
H="$(cfg)"
check "--device ROCm0 -> card 0" "0" \
  "$(run "$H" "print(llmreg.gpu_of([e for e in llmreg.parse_config() if e['name']=='big'][0])['device'])")"
check "card 0 is not None"       "single" \
  "$(run "$H" "print(llmreg.gpu_of([e for e in llmreg.parse_config() if e['name']=='big'][0])['mode'])")"
check "no flag -> both cards"    "both" \
  "$(run "$H" "print(llmreg.gpu_of([e for e in llmreg.parse_config() if e['name']=='spread'][0])['mode'])")"
check "env pin -> single"        "1" \
  "$(run "$H" "print(llmreg.gpu_of([e for e in llmreg.parse_config() if e['name']=='whisper'][0])['device'])")"
check "env pin gets a group"     "pinned" \
  "$(run "$H" "print(llmreg.gpu_of([e for e in llmreg.parse_config() if e['name']=='whisper'][0])['group'])")"
#  On an iGPU-first machine the env holds 1 absolute = card 0 logical.
check "env pin translated"       "0" \
  "$(LLM_HOME="$H" LLM_ROCM_SMI="$SMI_IGPU" LLM_DGPUS= LLM_MIN_VRAM_GB= \
     pyx "print(llmreg.gpu_of([e for e in llmreg.parse_config() if e['name']=='whisper'][0])['device'])")"

section "role_of"
check "chat"    "chat"   "$(run "$H" "print(llmreg.role_of('\${server} -m /a.gguf'))")"
check "embed"   "embed"  "$(run "$H" "print(llmreg.role_of('\${server-embed} -m /a.gguf --embedding'))")"
check "rerank"  "rerank" "$(run "$H" "print(llmreg.role_of('\${server-rerank} -m /a.gguf --reranking'))")"
check "stt"     "stt"    "$(run "$H" "print(llmreg.role_of('/bin/whisper-server -m /a.bin'))")"

# ---------------------------------------------------------------------------
section "slots_of: both spellings, and the auto default of four"
check "-np 4 -kvu"       "(4, True)"   "$(run "$H" "print(llmreg.slots_of('-np 4 -kvu'))")"
check "--parallel 1"     "(1, False)"  "$(run "$H" "print(llmreg.slots_of('--parallel 1'))")"
check "nothing = auto"   "(4, True)"   "$(run "$H" "print(llmreg.slots_of(''))")"
check "-np 2 alone"      "(2, False)"  "$(run "$H" "print(llmreg.slots_of('-np 2'))")"
check "-no-kvu wins"     "(4, False)"  "$(run "$H" "print(llmreg.slots_of('-np 4 -kvu -no-kvu'))")"
check "whisper has none" "(None, None)" "$(run "$H" "print(llmreg.slots_of('-np 4', 'stt'))")"

# ---------------------------------------------------------------------------
section "sync_groups"
H="$(cfg)"
check "one group, not one per card" "pinned" \
  "$(run "$H" "
import re
t = llmreg.sync_groups()
print(re.search(r'^  (\w+):', t[t.index('llm:groups'):], re.M).group(1))")"
check "card-pinned models are members" "big whisper" \
  "$(run "$H" "
import re
t = llmreg.sync_groups()
blk = t[t.index('>>> llm:groups'):t.index('<<< llm:groups')]
print(' '.join(sorted(re.findall(r'- \"([^\"]+)\"', blk))))")"
check "all-cards model stays out" "False" \
  "$(run "$H" "
t = llmreg.sync_groups()
blk = t[t.index('>>> llm:groups'):t.index('<<< llm:groups')]
print('spread' in blk)")"
check "persistent is set" "True" \
  "$(run "$H" "print('persistent: true' in llmreg.sync_groups())")"
check "idempotent" "True" \
  "$(run "$H" "t = llmreg.sync_groups(); print(llmreg.sync_groups(t) == t)")"
#  The regression: returning the text unchanged made every caller report success
#  while the pinned models silently stayed in llama-swap's swapping default group.
check "creates its marker when absent" "True" \
  "$(run "$H" "print('>>> llm:groups' in llmreg.sync_groups())")"
check "block lands before models:" "True" \
  "$(run "$H" "
import re
t = llmreg.sync_groups()
print(t.index('llm:groups') < re.search(r'^models:', t, re.M).start())")"
check_err "no models: section raises" ValueError \
  "$(run "$H" "llmreg.put_block('nothing: here\n', 'groups', '')" 2>/dev/null)"

# ---------------------------------------------------------------------------
section "roles: render/read round-trip"
H="$(cfg)"
ROLES="{'warm-one': {'strategy': 'warm', 'targets': ['big', 'spread']},
        'spill': {'strategy': 'spillover', 'targets': ['big', 'spread'],
                  'settings': {'spillover': 3}},
        'pinned-one': {'strategy': 'pin', 'targets': ['big'],
                       'description': 'the big one'}}"
check "round-trip is exact" "True" \
  "$(run "$H" "
sel = $ROLES
t = llmreg.write_selectors(sel, llmreg.config_text())
print(llmreg.read_selectors(t) == sel)")"
check "write is idempotent" "True" \
  "$(run "$H" "
sel = $ROLES
a = llmreg.write_selectors(sel, llmreg.config_text())
print(llmreg.write_selectors(llmreg.read_selectors(a), a) == a)")"
check "spillover count survives" "3" \
  "$(run "$H" "
sel = $ROLES
t = llmreg.write_selectors(sel, llmreg.config_text())
print(llmreg.read_selectors(t)['spill']['settings']['spillover'])")"
check "empty dict removes the key" "False" \
  "$(run "$H" "
t = llmreg.write_selectors({}, llmreg.config_text())
print('selectors:' in t)")"
check "roles land before models:" "True" \
  "$(run "$H" "
import re
t = llmreg.write_selectors($ROLES, llmreg.config_text())
print(t.index('llm:selectors') < re.search(r'^models:', t, re.M).start())")"

section "set_selector validation"
check_err "unknown strategy" ValueError "$(run "$H" "llmreg.set_selector('r','bogus',['big'],dry_run=True)" 2>/dev/null)"
check_err "no targets"       ValueError "$(run "$H" "llmreg.set_selector('r','warm',[],dry_run=True)" 2>/dev/null)"
check_err "unknown target"   ValueError "$(run "$H" "llmreg.set_selector('r','warm',['ghost'],dry_run=True)" 2>/dev/null)"
check_err "name collides"    ValueError "$(run "$H" "llmreg.set_selector('big','warm',['big'],dry_run=True)" 2>/dev/null)"
check "dry run writes nothing" "False" \
  "$(run "$H" "
llmreg.set_selector('r', 'warm', ['big'], dry_run=True)
print('llm:selectors' in llmreg.config_text())")"
check "real write persists" "warm" \
  "$(run "$H" "
llmreg.set_selector('r', 'warm', ['big'])
print(llmreg.read_selectors()['r']['strategy'])")"

# ---------------------------------------------------------------------------
section "a role never promises more than its weakest target"
H="$(cfg)"
check "context is the minimum" "65536" \
  "$(run "$H" "
llmreg.set_selector('mix', 'warm', ['big', 'spread'])
c = llmreg.catalog(with_live=False)
print(next(m for m in c if m['id'] == 'mix')['runtime']['contextWindow'])")"
check "role is tagged as such" "role" \
  "$(run "$H" "
llmreg.set_selector('mix', 'warm', ['big', 'spread'])
c = llmreg.catalog(with_live=False)
print(next(m for m in c if m['id'] == 'mix')['kind'])")"
check "models are tagged too" "model" \
  "$(run "$H" "print(llmreg.catalog(with_live=False)[0]['kind'])")"

# ---------------------------------------------------------------------------
#  A fresh sandbox per check: these write, and sharing one would make the order
#  of the assertions part of what is being tested.
section "patch_model"
check "context change lands in cmd" "32768" \
  "$(run "$(cfg)" "
llmreg.patch_model('big', {'contextWindow': 32768, 'force': True})
print(llmreg.flag(llmreg.get_model('big')['runtime']['cmd'], '-c'))")"
check "slots written canonically" "(2, True)" \
  "$(run "$(cfg)" "
llmreg.patch_model('big', {'parallel': 2, 'force': True})
print(llmreg.slots_of(llmreg.get_model('big')['runtime']['cmd']))")"
check "dry run leaves the file alone" "131072" \
  "$(run "$(cfg)" "
llmreg.patch_model('big', {'contextWindow': 4096, 'force': True}, dry_run=True)
print(llmreg.flag(llmreg.get_model('big')['runtime']['cmd'], '-c'))")"
#  Patching slots twice used to corrupt the line: set_flag(name, None) removed
#  the bare switch AND the token after it, so '-np 3 -kvu' became '3 -kvu' and
#  the next flag along was swallowed with it.
H="$(cfg)"
check "patching slots repeatedly stays valid" "(1, True) q8_0 ROCm0" \
  "$(run "$H" "
for n in (2, 3, 4, 1):
    llmreg.patch_model('big', {'parallel': n, 'force': True})
c = llmreg.get_model('big')['runtime']['cmd']
print(llmreg.slots_of(c), llmreg.flag(c, '-ctk'), llmreg.flag(c, '--device'))")"
check "exactly one -np survives" "1" \
  "$(run "$H" "print(llmreg.get_model('big')['runtime']['cmd'].count('-np'))")"
check "exactly one -kvu survives" "1" \
  "$(run "$H" "print(llmreg.get_model('big')['runtime']['cmd'].count('-kvu'))")"
check_err "whisper has no slots" ValueError \
  "$(run "$H" "llmreg.patch_model('whisper', {'parallel': 2}, dry_run=True)" 2>/dev/null)"
check_err "whisper cannot span cards" ValueError \
  "$(run "$H" "llmreg.patch_model('whisper', {'gpu': 'both'}, dry_run=True)" 2>/dev/null)"
check "moving a model regroups it" "False" \
  "$(run "$H" "
llmreg.patch_model('big', {'gpu': 'both', 'force': True})
t = llmreg.config_text()
print('\"big\"' in t[t.index('>>> llm:groups'):t.index('<<< llm:groups')])")"

# ---------------------------------------------------------------------------
section "sync_tensor_split follows the card count"
H="$(cfg)"
check "two cards -> 1,1" "-ts 1,1" \
  "$(run "$H" "
import re
t = llmreg.sync_tensor_split()
print((re.search(r'^\s*(-ts [\d.,]+)\s*$', t, re.M) or ['','none'])[1])")"
check "one card -> gone" "none" \
  "$(LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-1card.sh" LLM_DGPUS= LLM_MIN_VRAM_GB= \
     pyx "
import re
t = llmreg.sync_tensor_split()
m = re.search(r'^\s*(-ts [\d.,]+)\s*$', t, re.M)
print(m.group(1) if m else 'none')")"

summary
