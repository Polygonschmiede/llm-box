#!/usr/bin/env bash
# ============================================================================
#  Check that the control page actually renders
# ============================================================================
#  Why not just curl /ui: it answers 200 with the file whatever the JavaScript
#  does. The first version of this check passed while every element on the page
#  read [object Object].
#
#  So the page's script is run under node against payloads generated from a
#  throwaway LLM_HOME - not from the machine this runs on - with a minimal DOM
#  in tests/dom-stub.js. Skipped when node or venv-api is missing.
#
#  Run with:  bash tests/ui-matrix.sh      (or: bash tests/run-all.sh)
# ============================================================================
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

if ! command -v node >/dev/null 2>&1; then
  skip "the whole page suite" "node is not installed"
  summary; exit $?
fi
if ! have_api; then
  skip "the whole page suite" "venv-api is missing (llm setup)"
  summary; exit $?
fi

#  A sandbox with something of everything the page has to render: a pinned
#  model with weight behind it, one spread over all cards, an embedder, an
#  env-pinned whisper, a role over two of them, and a generated group block.
H="$(sandbox template)"          # the shipped template, so all five macros exist
sed -i "s|@LLM_HOME@|$H|g; s|@WHISPER_HOME@|$H/whisper.cpp|g" "$H/config/llama-swap.yaml"
cp "$REPO/VERSION" "$H/VERSION"  # a real checkout has one; the page shows it
#  Sparse files: vram.weightsBytes comes from the file size, and without one the
#  page has nothing to weigh - which is most of what it exists to show.
mkdir -p "$H/models/big" "$H/models/spread"
truncate -s 24G "$H/models/big/big.gguf"
truncate -s 40G "$H/models/spread/spread.gguf"
add_block "$H" big \
  "\${server} -m $H/models/big/big.gguf -c 131072 -ctk q8_0 --reasoning-effort low --device ROCm0 -sm none -mg 0"
add_block "$H" spread "\${server} -m $H/models/spread/spread.gguf -c 65536"
add_block "$H" embed  '${server-embed} -m /home/x/models/embed/e.gguf -c 4096 --embedding'
add_block "$H" whisper '/bin/whisper-server -m /home/x/models/w/w.bin --request-path /v1/audio' \
  '    env:
      - "HIP_VISIBLE_DEVICES=1"'
printf 'testtoken\n' > "$H/config/api-token"
LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
  pyx "
llmreg.set_selector('mix', 'warm', ['big', 'spread'])
llmreg._write_config(llmreg.sync_groups())
#  Without a sidecar every model reports 'provenance unknown', and the check for
#  it would pass on the fallback text instead of the real thing.
llmreg.write_meta('$H/models/big', {'repo': 'unsloth/Fixture-7B-GGUF', 'quant': 'Q4_K_M',
                                    'revision': 'abcdef0123456789', 'verified': True})" >/dev/null

#  Dump exactly what the page fetches, through the real app.
FX="$TMP/ui-fx"; mkdir -p "$FX"
LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
LLM_SWAP_API="http://127.0.0.1:9" pyapi "
import importlib.util, json, pathlib
spec = importlib.util.spec_from_file_location('llmapi', '$REPO/bin/llm-api.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
from fastapi.testclient import TestClient
c = TestClient(mod.app)
c.post('/api/session', json={'token': 'testtoken'})
for name, path in (('session', '/api/session'), ('models', '/api/models'),
                   ('gpus', '/api/gpus'), ('versions', '/api/versions'),
                   ('config', '/api/config'), ('diff', '/api/config/diff'),
                   ('roles', '/api/roles'), ('jobs', '/api/jobs')):
    r = c.get(path)
    if r.status_code != 200:
        raise SystemExit('%s -> HTTP %d' % (path, r.status_code))
    pathlib.Path('$FX/%s.json' % name).write_text(json.dumps(r.json()))
print('ok')" >/dev/null || { skip "the whole page suite" "could not produce payloads"; summary; exit $?; }

#  The script out of the single-file page.
PAGE="$TMP/page.js"
python3 -c "
import re, pathlib, sys
h = pathlib.Path('$REPO/web/index.html').read_text()
m = re.search(r'<script>(.*)</script>', h, re.S)
if not m:
    sys.exit('no <script> block in web/index.html')
pathlib.Path('$PAGE').write_text(m.group(1))"

section "no external resources"
check "nothing is fetched from another host" "0" \
  "$(grep -coE '(src|href)="https?://' "$REPO/web/index.html" || true)"
check "the script is inline" "1" \
  "$(grep -c '<script>' "$REPO/web/index.html")"

#  One check, because the node script either gets through all of it or does
#  not; the lines it prints are the detail, not separate assertions here.
section "the page renders against a generated configuration"
checks=$((checks + 1))
if node "$REPO/tests/render-checks.js" "$FX" "$PAGE"; then
  printf '  \033[0;32mok\033[0m    %s\n' "every render check above passed"
else
  fails=$((fails + 1))
fi

summary
