#!/usr/bin/env bash
# ============================================================================
#  What has to be true of the REPOSITORY, not of the code
# ============================================================================
#  Four guards that have nothing to do with behaviour and everything to do with
#  this tree staying usable by someone who is not on this machine:
#  no leftover German, no machine-specific paths, no secret ever tracked, and
#  one version number rather than three that agree by hand.
#
#  These used to live inline in .github/workflows/ci.yml, where a contributor
#  could only discover them by pushing and going red. They are git-based, so a
#  tarball without .git skips them rather than failing.
#
#  Run with:  bash tests/repo-matrix.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
# shellcheck source=tests/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

SELF=tests/repo-matrix.sh

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  skip "repository guards" "not a git checkout"
  summary; exit $?
fi

section "language"

#  The tree was translated once and the residue kept turning up in user-facing
#  strings - an HTTP 400 body, a diff label, two pi menu items. A short explicit
#  word list is cheap and catches the regression; a general language detector
#  would only produce false positives. CHANGELOG.md is exempt because it quotes
#  what the strings used to say.
GERMAN='[äöüÄÖÜß]|\b(Karte|Grafikkarte|Modelle|Fortschritt|enthaelt|unerlaubte|Dienst|riesige|ueberspringen|Zeichen|neu\))'
hits=$(git grep -nIE "$GERMAN" -- ":!CHANGELOG.md" ":!$SELF" | head -5)
check "no German in a tracked file" "" "$hits"

section "portability"

#  A home directory or a private address in a tracked file means the tree only
#  works on the machine it was written on. Documentation is checked too: the
#  install guide is exactly where such a path would look plausible.
PATHS='/home/[a-z][a-z0-9_-]+/|(^|[^0-9.])(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)'
hits=$(git grep -nIE "$PATHS" -- ":!$SELF" | head -5)
check "no machine path or private address" "" "$hits"

section "secrets"

#  git ls-files, not the filesystem: this asks what is IN the repository, so
#  loosening .gitignore cannot quiet it. Every one of these is generated per
#  machine and two of them are credentials.
SECRETS=(config/api-token config/api-key config/api-key.env
         config/llama-swap.yaml config/hardware.env config/comfyui.env)
tracked=""
for f in "${SECRETS[@]}"; do
  [[ -n "$(git ls-files -- "$f")" ]] && tracked="$tracked $f"
done
check "no per-machine config is tracked" "" "$tracked"
check "no model weights are tracked" "" \
  "$(git ls-files -- '*.gguf' '*.llm-model.json' | head -3)"

section "documentation"

#  Not inline python this time. As a heredoc inside the workflow this check read
#  its own program from stdin and the piped file list went nowhere, so it ran
#  zero times and reported success for every push since it was written.
mapfile -t mds < <(git ls-files '*.md')
out=$(python3 tests/check-links.py "${mds[@]}" 2>&1)
check "every relative link resolves" "0 broken" \
  "$(printf '%s' "$out" | sed -n 's/.*, \([0-9]* broken\)$/\1/p')"

section "one version number"

ver_file=$(cat VERSION 2>/dev/null)
ver_pkg=$(sed -n 's/^  "version": "\([^"]*\)".*/\1/p' package.json)
#  The topmost heading that is a release, i.e. not [Unreleased].
ver_log=$(grep -m1 -oE '^## \[[0-9]+\.[0-9]+\.[0-9]+\]' CHANGELOG.md | tr -d '#[] ')
check "VERSION and package.json agree" "$ver_file" "$ver_pkg"
check "VERSION and the changelog agree" "$ver_file" "$ver_log"

summary
