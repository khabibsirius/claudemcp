/**
 * Extract the real default property tree for every Qlik chart type.
 *
 *   node tools/extract_chart_specs.js > tools/chart_defaults.json
 *
 * Qlik's own client ships each visualization as a nebula.js bundle under
 *   <qlik>/Client/qmfe/@nebula.js/sn-<name>/<version>/dist/sn-<name>.js
 * and each one exports `qae.properties` - exactly the property tree the
 * client writes when you add that chart by hand - plus `qae.data.targets`,
 * which says how many dimensions and measures it accepts.
 *
 * This matters because guessing these has failed repeatedly: the pie chart
 * rendered as an empty box for want of `donut`, and the table for want of
 * `columnOrder`. Neither errored. Reading the client's own definition is the
 * only way to get them right without hand-building all 22 charts in Qlik.
 *
 * The bundles are browser code, so they need a DOM (jsdom) and a stub for
 * their stardust peer dependency; neither is used to produce the static
 * definition we want.
 */

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const QLIK_CLIENT =
  process.env.QLIK_CLIENT || "C:/qlik/Client/qmfe/@nebula.js";

// --- browser environment -------------------------------------------------

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  pretendToBeVisual: true,
  url: "http://localhost/",
});

global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.location = dom.window.location;
global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element;
global.Node = dom.window.Node;
global.Image = dom.window.Image;
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = clearTimeout;
global.self = global.window;

// Browser APIs jsdom doesn't provide. Each of these was an actual failure:
// sessionStorage (line chart), ResizeObserver (table, pivot table), canvas
// (anything that measures text).
const storage = {
  getItem: () => null, setItem() {}, removeItem() {}, clear() {}, key: () => null, length: 0,
};
global.sessionStorage = dom.window.sessionStorage = storage;
global.localStorage = dom.window.localStorage = storage;

class Observer {
  observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
}
global.ResizeObserver = dom.window.ResizeObserver = Observer;
global.IntersectionObserver = dom.window.IntersectionObserver = Observer;
global.MutationObserver = global.MutationObserver || Observer;

global.matchMedia = dom.window.matchMedia = () => ({
  matches: false, media: "", addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {},
});

// jsdom throws on getContext without the native canvas package; charts only
// use it to measure text, so a stub with plausible metrics is enough.
dom.window.HTMLCanvasElement.prototype.getContext = () => ({
  measureText: (t) => ({ width: String(t).length * 7 }),
  fillText() {}, save() {}, restore() {}, scale() {}, translate() {},
  clearRect() {}, fillRect() {}, beginPath() {}, closePath() {}, stroke() {},
  fill() {}, arc() {}, moveTo() {}, lineTo() {}, setTransform() {},
  createLinearGradient: () => ({ addColorStop() {} }),
});

// Anything-goes stand-in for @nebula.js/stardust: callable, and every
// property access returns itself, so whatever shape a bundle expects at
// import time it gets something rather than undefined.
const any = new Proxy(function () {}, {
  get: () => any,
  apply: () => any,
  construct: () => any,
});

const Module = require("module");
const load = Module._load;
Module._load = function (request) {
  if (String(request).includes("stardust")) return any;
  return load.apply(this, arguments);
};

// --- extraction ----------------------------------------------------------

function bundleFor(name) {
  const dir = path.join(QLIK_CLIENT, name);
  if (!fs.existsSync(dir)) return null;
  for (const version of fs.readdirSync(dir)) {
    const file = path.join(dir, version, "dist", `${name}.js`);
    if (fs.existsSync(file)) return { file, version };
  }
  return null;
}

/** Strip functions and cycles - only JSON-serialisable data is useful. */
function plain(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object") {
    return typeof value === "function" ? undefined : value;
  }
  if (seen.has(value)) return undefined;
  seen.add(value);

  if (Array.isArray(value)) {
    return value.map((v) => plain(v, seen)).filter((v) => v !== undefined);
  }
  const out = {};
  for (const key of Object.keys(value)) {
    let child;
    try {
      child = plain(value[key], seen);
    } catch {
      continue;
    }
    if (child !== undefined) out[key] = child;
  }
  return out;
}

/**
 * Targets declare min/max as either a number or a function returning one.
 * Serialising them naively drops the functions, which reported "barchart
 * accepts 0 dimensions" - a rule that would reject the charts that already
 * work. Call them where they are callable.
 */
function resolveCount(spec) {
  if (!spec || typeof spec !== "object") return undefined;
  const out = {};
  for (const key of ["min", "max"]) {
    const value = spec[key];
    if (typeof value === "function") {
      try { out[key] = value(); } catch { /* leave unset */ }
    } else if (typeof value === "number") {
      out[key] = value;
    }
  }
  return out;
}

function resolveTargets(targets) {
  return (targets || []).map((t) => ({
    path: t && t.path,
    dimensions: resolveCount(t && t.dimensions),
    measures: resolveCount(t && t.measures),
  }));
}

const results = {};
const failures = {};

for (const name of fs.readdirSync(QLIK_CLIENT)) {
  if (!name.startsWith("sn-")) continue;
  const bundle = bundleFor(name);
  if (!bundle) continue;

  try {
    const mod = require(bundle.file);
    const sn = mod.default || mod;
    // The galaxy argument, not an empty object: bundles immediately call
    // flags.isEnabled(), translator.get() and read env.anything.sense on
    // it. Passing {} is what produced every
    // "Cannot read properties of undefined" failure.
    const definition = typeof sn === "function" ? sn(any) : sn;
    const qae = definition && definition.qae;

    if (!qae || !qae.properties) {
      failures[name] = "no qae.properties";
      continue;
    }

    results[name] = {
      version: bundle.version,
      // What the chart calls itself to the engine, e.g. "combochart".
      type: (qae.properties.qInfo && qae.properties.qInfo.qType) || null,
      properties: plain(qae.properties),
      targets: resolveTargets((qae.data && qae.data.targets) || []),
    };
  } catch (e) {
    failures[name] = String(e.message).slice(0, 120);
  }
}

process.stdout.write(
  JSON.stringify({ extracted: results, failures }, null, 1)
);
console.error(
  `extracted ${Object.keys(results).length}, failed ${Object.keys(failures).length}`
);
process.exit(0);
