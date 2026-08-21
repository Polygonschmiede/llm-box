// ===========================================================================
//  A minimal DOM, so web/index.html's render functions can run under node
// ===========================================================================
//  Not a browser. Enough to catch a render path that throws, a field that never
//  reaches the page, or a number formatted as [object Object] - none of which an
//  HTTP 200 on /ui would notice, and there is no headless browser in this
//  project's dependency budget.
//
//  Used by tests/ui-matrix.sh, which feeds it payloads generated from a
//  throwaway LLM_HOME rather than from the machine it runs on.
const fs = require("fs");
const FX = process.argv[2];

class N {
  constructor(tag) { this.tagName = (tag || "").toUpperCase(); this.children = [];
    this.attrs = {}; this._text = ""; this.listeners = {};
    //  el() in the page does `k.nodeType ? k : createTextNode(k)`, so a node
    //  without nodeType gets wrapped as text and renders as [object Object].
    this.nodeType = 1; }
  set className(v) { this.attrs.class = v; } get className() { return this.attrs.class || ""; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map(c => c.textContent === undefined ? String(c) : c.textContent).join("");
  }
  //  el() writes `hidden: true` as setAttribute("hidden", "") - which hides the
  //  element in a browser, and read back through !! used to come out FALSE
  //  here. Presence is what counts, not truthiness.
  set hidden(v) { this.attrs.hidden = !!v; }
  get hidden() { return this.attrs.hidden !== undefined && this.attrs.hidden !== false; }
  set onkeydown(f) { this.listeners.keydown = f; }
  set onclick(f) { this.listeners.click = f; }
  set onchange(f) { this.listeners.change = f; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(k, f) { (this.listeners[k] = this.listeners[k] || []).push(f); }
  append(...k) { for (const x of k) { if (x instanceof N) x.parent = this;
                                      this.children.push(x); } }
  replaceChildren(...k) { this.children = []; this._text = ""; this.append(...k); }
  showModal() { this.open = true; } close() { this.open = false; }
  get dataset() { return this.attrs; }
  querySelector(sel) { return find(this, sel); }
  querySelectorAll(sel) { return findAll(this, sel); }
  //  Walks up for real, so the details toggle - closest(".card") then
  //  querySelector(".body") - and the tooltip delegate both take the same path
  //  here as in a browser. It used to return `this` unconditionally, which made
  //  every one of those look like it worked.
  closest(sel) {
    for (let n = this; n instanceof N; n = n.parent)
      if (!sel || matches(n, sel)) return n;
    return null;
  }
  focus() {}
  //  No layout in here, so a rectangle of zeros. Enough for the tooltip's
  //  placement arithmetic to run without throwing, which is what is being
  //  tested - not where the box lands.
  getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0,
                                     width: 0, height: 0 }; }
  get offsetWidth() { return 0; } get offsetLeft() { return 0; }
  get clientLeft() { return 0; }
  get style() { return this._style = this._style
    || { setProperty(k, v) { this[k] = v; } }; }
  get scrollHeight() { return 0; } set scrollTop(v) {}
}
function walk(n, fn) {
  if (!(n instanceof N)) return;
  fn(n); for (const c of n.children) walk(c, fn);
}
function matches(n, sel) {
  if (sel.startsWith("#")) return n.attrs.id === sel.slice(1);
  if (sel.startsWith(".")) return (n.attrs.class || "").split(/\s+/).includes(sel.slice(1));
  //  A bare attribute selector, which is how the tooltip finds its targets.
  if (sel.startsWith("[")) {
    const a = sel.slice(1, -1).split("=");
    return a[1] === undefined ? n.attrs[a[0]] !== undefined
      : String(n.attrs[a[0]]) === a[1].replace(/^"|"$/g, "");
  }
  const m = sel.match(/^(\w+)(?:\[([^=\]]+)(?:="?([^"\]]*)"?)?\])?$/);
  if (!m) return false;
  if (n.tagName !== m[1].toUpperCase()) return false;
  if (m[2] && String(n.attrs[m[2]]) !== String(m[3])) return false;
  return true;
}
function find(root, sel) { let r = null; walk(root, n => { if (!r && matches(n, sel)) r = n; }); return r; }
function findAll(root, sel) { const o = []; walk(root, n => { if (matches(n, sel)) o.push(n); }); return o; }

const doc = new N("html");
for (const id of ["ver", "who", "auth", "reload", "banner",
                  "tab-models", "tab-roles", "tab-cards", "tab-system", "dlg"]) {
  const n = new N(id === "dlg" ? "dialog" : id.startsWith("tab-") ? "section" : "span");
  n.attrs.id = id; doc.children.push(n);
}
for (const t of ["models", "roles", "cards", "system"]) {
  const b = new N("button"); b.attrs["data-tab"] = t; b.attrs.class = "navbtn";
  doc.children.push(b);
}
//  The page appends its one tooltip node here and hangs its listeners off the
//  document, so both have to exist or tipInit() bails out and the whole tooltip
//  path goes untested.
const body = new N("body");
doc.children.push(body);
global.document = {
  documentElement: doc,
  body: body,
  addEventListener: () => {},
  createElement: t => new N(t),
  createTextNode: t => ({ textContent: String(t), nodeType: 3 }),
  querySelector: s => find(doc, s),
  querySelectorAll: s => s === "nav button" ? findAll(doc, "button[class=navbtn]")
                        : s === "main section" ? findAll(doc, "section") : findAll(doc, s),
};
global.window = { innerWidth: 1280, innerHeight: 800, addEventListener: () => {} };
global.location = { protocol: "http:", hostname: "127.0.0.1" };
global.setInterval = () => 0;
global.clearInterval = () => {};
global.requestAnimationFrame = f => { f(); return 0; };
const MAP = { "/api/session": "session", "/api/models": "models", "/api/gpus": "gpus",
              "/api/versions": "versions", "/api/config": "config",
              "/api/config/diff": "diff", "/api/roles": "roles", "/api/jobs": "jobs" };
global.fetch = async (p) => {
  const k = MAP[p];
  if (!k) return { ok: false, status: 404, statusText: "no fixture", text: async () => "{}" };
  const body = fs.readFileSync(`${FX}/${k}.json`, "utf8");
  return { ok: true, status: 200, statusText: "OK", text: async () => body };
};
module.exports = { doc, N };
