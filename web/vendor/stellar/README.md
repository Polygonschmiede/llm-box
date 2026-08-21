# Stellar DS, vendored

`@polygonschmied/stellar-tokens` **0.18.0** — the design tokens and base
component CSS the control page is built on.

| File | Upstream path | Bytes |
|---|---|---|
| `index.css` | `dist/index.css` | 77715 |
| `auto-dark.css` | `dist/auto-dark.css` | 4544 |

Both are **byte-identical copies**. Nothing here is edited — not even a
provenance header — so an update is a copy and never a merge. Everything this
project adds sits in the `<style>` block of `../../index.html`, which is
unlayered and therefore wins over the whole `@layer stellar` stack regardless of
load order.

`dist/index.css` is the pre-bundled token *and* component layer: no `@import`,
no source-map comment, nothing to resolve. That is why it is this one file
rather than the six sub-exports.

## Why vendored at all

This project installs llama.cpp and two venvs. A page that pulled from npm at
build time would be the only reason in the tree to own a JavaScript toolchain,
and one that pulled from a CDN at run time would stop working on a machine
without internet access — which is most of the point of running models locally.
So the CSS lives here, and `bin/llm-api.py` serves it from the same origin as
the API.

## Updating

```sh
V=0.18.0
B=https://registry.npmjs.org/@polygonschmied/stellar-tokens/-/stellar-tokens-$V.tgz
curl -sfL "$B" | tar -xzO package/dist/index.css     > web/vendor/stellar/index.css
curl -sfL "$B" | tar -xzO package/dist/auto-dark.css > web/vendor/stellar/auto-dark.css
```

Then update the version and the byte counts above, and run
`bash tests/ui-matrix.sh` — it checks that the page still renders and that
nothing in here reaches for an external host.

Watch for two things when the major or minor moves: a renamed component class
(the page uses `stl-btn`, `stl-card`, `stl-card__body`, `stl-tag` and its colour
modifiers, `stl-banner`, `stl-progress`, `stl-tabs__list`, `stl-tabs__trigger`,
`stl-dialog`, `stl-input`, `stl-tooltip`, `eyebrow`, `stl-text-*`) and a renamed
token (`--paper-*`, `--ink-*`, `--ember`, `--citron-*`, `--crimson`, `--kelp`,
`--sky`, `--space-*`, `--r-*`, `--fs-*`, `--dur-*`, `--ease-*`, `--shadow-*`).

Upstream: <https://github.com/Polygonschmiede/stellar-ds> · MIT.
