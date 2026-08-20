// ===========================================================================
//  What web/index.html must actually put on the page
// ===========================================================================
//  Run by tests/ui-matrix.sh:  node tests/render-checks.js <fixtures> <page.js>
//
//  These assert on the rendered TEXT, not on the code. The page can return
//  HTTP 200 and still show nothing - it did, while the DOM stub was missing
//  nodeType and every element ended up wrapped as [object Object].
const { doc } = require("./dom-stub.js");
const fs = require("fs");

//  load() is async and the page calls it itself, so a throw inside it surfaces
//  as an unhandled rejection and node exits before the checks run. Report it as
//  a failure rather than a stack trace with no context.
process.on("unhandledRejection", err => {
  console.log("  \x1b[0;31mFAIL\x1b[0m  the page threw while loading: "
              + (err && err.message));
  console.log((err && err.stack || "").split("\n").slice(1, 4)
              .map(l => "        " + l.trim()).join("\n"));
  process.exit(1);
});

let src = fs.readFileSync(process.argv[3], "utf8").replace(/^"use strict";/, "");

(async () => {
  try {
    eval(src);                                    // the page calls load() itself
    await new Promise(r => setTimeout(r, 80));
    const q = s => doc.querySelector(s).textContent;
    const models = q("#tab-models"), roles = q("#tab-roles");
    const cards = q("#tab-cards"), system = q("#tab-system");
    const checks = [
      ["the header carries a version", /^v\d+\.\d+\.\d+$/.test(q("#ver")), q("#ver")],
      ["nothing failed to load", q("#banner") === "", q("#banner").slice(0, 90)],
      ["the session state shows", q("#who") === "signed in", q("#who")],
      ["models are listed", ["big", "spread", "embed", "whisper"]
        .every(m => models.includes(m)), ""],
      ["roles are not listed as models", !/^Models\s+mix/.test(models), ""],
      //  Estimated against free, per card. The sum across cards is the wrong
      //  number and check_fit() already learned that once.
      ["VRAM is estimated and compared", /estimated/.test(models)
        && /free right now|tightest is card/.test(models), ""],
      ["a spread model shows the per-card share", /on each of \d+ cards/.test(models), ""],
      ["slots are explained, not just counted", /shared pool|hard \d/.test(models), ""],
      //  Not /Came from/: that label is printed for "provenance unknown" too,
      //  so the check passed while the real branch was disabled.
      ["provenance names the repo", /unsloth\/Fixture-7B-GGUF/.test(models), ""],
      ["and the quant and revision", /Q4_K_M/.test(models)
        && /abcdef012345/.test(models), ""],
      ["the command line is shown", /\$\{server/.test(models), ""],
      ["write actions appear when signed in", /remove/.test(models)
        && /change:/.test(models), ""],
      ["roles show their effective context", /ctx/.test(roles)
        && /mix/.test(roles), ""],
      ["the weakest-target rule is stated", /SMALLEST/.test(roles), ""],
      ["cards are listed", /card 0/.test(cards), ""],
      ["groups show their eviction flags", /persistent/.test(cards), ""],
      ["macros are shown", /server-embed|server-mtp|llama-server/.test(cards), ""],
      ["config drift is reported either way", /matches|no longer matches/.test(cards), ""],
      ["versions are listed", /llama\.cpp/.test(system) && /llama-swap/.test(system), ""],
      ["the other interfaces are linked", /8080\/ui|llama-swap on/.test(system), ""],
    ];
    let bad = 0;
    for (const [name, ok, extra] of checks) {
      const mark = ok ? "\x1b[0;32mok\x1b[0m  " : "\x1b[0;31mFAIL\x1b[0m";
      console.log(`  ${mark}  ${name.padEnd(46)}${extra}`);
      if (!ok) bad++;
    }
    process.exit(bad ? 1 : 0);
  } catch (e) {
    console.log("  \x1b[0;31mFAIL\x1b[0m  the page threw: " + (e && e.message));
    console.log((e && e.stack || "").split("\n").slice(1, 4).map(l => "        " + l).join("\n"));
    process.exit(1);
  }
})();
