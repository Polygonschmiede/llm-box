// ===========================================================================
//  What web/index.html must actually put on the page
// ===========================================================================
//  Run by tests/ui-matrix.sh:  node tests/render-checks.js <fixtures> <page.js>
//
//  These assert on the rendered TEXT, not on the code. The page can return
//  HTTP 200 and still show nothing - it did, while the DOM stub was missing
//  nodeType and every element ended up wrapped as [object Object].
const { doc, N } = require("./dom-stub.js");
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
    //  Tooltips live in an attribute, so textContent cannot see them. Collect
    //  "<the visible text> :: <the explanation>" pairs from a subtree instead.
    const tips = sel => {
      const out = [];
      (function walk(n) {
        if (!(n instanceof N)) return;
        if (n.attrs["data-tip"]) out.push(n.textContent + " :: " + n.attrs["data-tip"]);
        for (const c of n.children) walk(c);
      })(doc.querySelector(sel));
      return out.join("\n");
    };
    const classes = sel => {
      const out = [];
      (function walk(n) {
        if (!(n instanceof N)) return;
        if (n.attrs.class) out.push(n.attrs.class);
        for (const c of n.children) walk(c);
      })(doc.querySelector(sel));
      return out.join(" ");
    };
    const modelTips = tips("#tab-models"), cardTips = tips("#tab-cards");
    //  Click "details" on the first model, then trigger the refresh the way the
    //  page does - through the header's own button, which calls load() - and see
    //  whether the body is still open. The stub records listeners, so both the
    //  toggle and the re-render are the real handlers.
    async function expandSurvivesRefresh() {
      const openBody = () => findIn(doc.querySelector("#tab-models"),
        n => (n.attrs.class || "").split(/\s+/).includes("body"));
      const btn = findIn(doc.querySelector("#tab-models"),
        n => n.tagName === "BUTTON" && n.textContent === "details");
      const body = openBody();
      if (!btn || !body || body.hidden !== true) return false;
      for (const f of [btn.listeners.click].flat().filter(Boolean)) f({ target: btn });
      if (body.hidden) return false;                       // the click did nothing
      const reload = doc.querySelector("#reload");
      for (const f of [reload.listeners.click].flat().filter(Boolean)) f({ target: reload });
      await new Promise(r => setTimeout(r, 80));
      const again = openBody();
      return !!again && again.hidden === false;
    }
    function findIn(root, pred) {
      let hit = null;
      (function walk(n) {
        if (hit || !(n instanceof N)) return;
        if (pred(n)) { hit = n; return; }
        for (const c of n.children) walk(c);
      })(root);
      return hit;
    }
    const expandSurvives = await expandSurvivesRefresh();
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
      ["a newer version is offered", /newer: b10001/.test(system)
        && /update/.test(system), ""],
      ["a rollback is offered", /can go back to b9999/.test(system)
        && /back/.test(system), ""],
      ["and both can be started here", /check now/.test(system)
        && /update everything/.test(system), ""],
      ["the other interfaces are linked", /8080\/ui|llama-swap on/.test(system), ""],
      //  The design system is actually applied, not just linked: a card that
      //  renders without .stl-card means the class mapping was missed.
      ["the design system's classes are applied",
        /\bstl-card\b/.test(classes("#tab-models"))
        && /\bstl-btn\b/.test(classes("#tab-models"))
        && /\bstl-tag\b/.test(classes("#tab-models")), ""],
      //  Every abbreviation on the page is supposed to explain itself. These
      //  four are the ones that come from four different code paths: a row
      //  label, a value that is jargon, a generated quant sentence, and a flag
      //  picked out of a command line.
      ["row labels explain themselves", /^Slots :: .+queue/m.test(modelTips), ""],
      ["and so do the values", /^q8_0 :: .+KV cache/m.test(modelTips)
        || /^f16 \(unquantised\) :: /m.test(modelTips), ""],
      ["the quant is spelled out", /^Q4_K_M :: .+4 bits/m.test(modelTips), ""],
      //  -m and -c, not -ngl: the fixture's cmd references ${server}, so the
      //  macro's own flags are not in this string at all.
      ["and the flags in the command line", /^-c :: [Cc]ontext size/m.test(modelTips)
        && /^-m :: The weights file/m.test(modelTips), ""],
      ["an unknown term stays plain text", !/:: (null|undefined)/.test(modelTips), ""],
      //  The 15-second refresh re-renders the whole tab. It used to rebuild the
      //  detail body with hidden:true every time, so an expanded card closed
      //  itself within 15 seconds - see expandSurvivesRefresh below.
      ["an expanded card survives a refresh", expandSurvives, ""],
      //  Watts and utilisation, next to the junction temperature they arrive
      //  with. All three come from one rocm-smi query.
      ["cards report power and utilisation", /\d+(\.\d+)? W/.test(cards)
        && /busy \d+ %/.test(cards), ""],
      ["and both say what they mean", /:: Graphics package power/.test(cardTips)
        && /:: The share of time/.test(cardTips), ""],
      //  Which backend answered decides how every number on this tab was read,
      //  so the tab says it - and the explanation is the one for the backend in
      //  force, not a generic sentence covering both.
      ["the cards tab names the backend", /backend rocm/.test(cards), ""],
      ["and explains what that means here",
        /^backend rocm :: ROCm: one rocm-smi query/m.test(cardTips), ""],
      //  The device prefix in the prose follows the backend too. Under Vulkan
      //  this sentence has to read --device VulkanN, and a hardcoded "ROCmN"
      //  would still pass a check that only looked for the word "device".
      ["the logical-number note names the right prefix",
        /--device ROCmN/.test(cards) && !/--device VulkanN/.test(cards), ""],
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
