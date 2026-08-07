const { JSDOM } = require("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true, url: "http://localhost/" });
for (const k of ["window","document","navigator","location","HTMLElement","Element","Node","Image","getComputedStyle"]) global[k] = dom.window[k] || dom.window;
global.self = dom.window;
global.requestAnimationFrame = (cb)=>setTimeout(cb,0); global.cancelAnimationFrame = clearTimeout;
const st = {getItem:()=>null,setItem(){},removeItem(){},clear(){},key:()=>null,length:0};
global.sessionStorage = st; global.localStorage = st;
class O { observe(){} unobserve(){} disconnect(){} takeRecords(){return [];} }
global.ResizeObserver = O; global.IntersectionObserver = O;
dom.window.HTMLCanvasElement.prototype.getContext = () => ({ measureText: t=>({width:String(t).length*7}) });

const any = new Proxy(function(){}, { get:()=>any, apply:()=>any, construct:()=>any });
const Module = require("module"); const load = Module._load;
Module._load = function (r) { if (String(r).includes("stardust")) return any; return load.apply(this, arguments); };

try {
  const m = require("C:/qlik/Client/qmfe/@nebula.js/sn-pie-chart/2.3.0/dist/sn-pie-chart.js");
} catch (e) {
  console.log("MESSAGE:", e.message);
  console.log((e.stack||"").split("\n").slice(1,5).join("\n").slice(0,600));
}
