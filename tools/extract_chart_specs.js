const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const QLIK_CLIENT =
  process.env.QLIK_CLIENT || "C:/qlik/Client/qmfe/@nebula.js";

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

dom.window.HTMLCanvasElement.prototype.getContext = () => ({
  measureText: (t) => ({ width: String(t).length * 7 }),
  fillText() {}, save() {}, restore() {}, scale() {}, translate() {},
  clearRect() {}, fillRect() {}, beginPath() {}, closePath() {}, stroke() {},
  fill() {}, arc() {}, moveTo() {}, lineTo() {}, setTransform() {},
  createLinearGradient: () => ({ addColorStop() {} }),
});

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

function bundleFor(name) {
  const dir = path.join(QLIK_CLIENT, name);
  if (!fs.existsSync(dir)) return null;
  for (const version of fs.readdirSync(dir)) {
    const file = path.join(dir, version, "dist", `${name}.js`);
    if (fs.existsSync(file)) return { file, version };
  }
  return null;
}

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

function resolveCount(spec) {
  if (!spec || typeof spec !== "object") return undefined;
  const out = {};
  for (const key of ["min", "max"]) {
    const value = spec[key];
    if (typeof value === "function") {
      try { out[key] = value(); } catch {  }
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

    const definition = typeof sn === "function" ? sn(any) : sn;
    const qae = definition && definition.qae;

    if (!qae || !qae.properties) {
      failures[name] = "no qae.properties";
      continue;
    }

    results[name] = {
      version: bundle.version,

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
