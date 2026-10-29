/* Zero-dependency helpers shared by every viewport.  All DOM is built from text nodes
   (never from markup strings), so job ids, filenames and model tags cannot inject markup. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  function add(e, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      e.append(k.nodeType ? k : document.createTextNode(String(k)));
    }
  }
  function set(e, attrs) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.setAttribute('class', v);
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
  }
  const h = (tag, attrs, ...kids) => { const e = document.createElement(tag); set(e, attrs); add(e, kids); return e; };
  const s = (tag, attrs, ...kids) => { const e = document.createElementNS('http://www.w3.org/2000/svg', tag); set(e, attrs); add(e, kids); return e; };
  function errText(d, status) {
    if (!d || !d.detail) return 'HTTP ' + status;
    const x = d.detail;
    if (typeof x === 'string') return x;
    if (Array.isArray(x)) return x.map(i => i.msg).join('; ');
    if (x.message) return x.message + (x.purchase_sticks && Object.keys(x.purchase_sticks).length ? ' (order: ' + JSON.stringify(x.purchase_sticks) + ')' : '');
    if (x.missing_items) return 'Not ready: ' + x.missing_items.filter(m => m.severity === 'critical_blocker').length + ' blocker(s) open';
    return 'HTTP ' + status;
  }
  async function api(path, opt = {}) {
    const o = { method: opt.method || 'GET', headers: {} };
    if (opt.body instanceof FormData) o.body = opt.body;
    else if (opt.body !== undefined) { o.headers['content-type'] = 'application/json'; o.body = JSON.stringify(opt.body); }
    const r = await fetch(path, o);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) { const e = new Error(errText(data, r.status)); e.status = r.status; e.detail = data && data.detail; throw e; }
    return data;
  }
  const gcd = (a, b) => (b ? gcd(b, a % b) : a);
  function fmtIn(x) {
    const n = Math.round(x * 16), w = Math.floor(n / 16), f = n % 16;
    if (!f) return w + '"';
    const g = gcd(f, 16);
    return (w ? w + ' ' : '') + f / g + '/' + 16 / g + '"';
  }
  const money = n => '$' + Number(n).toFixed(2);
  function toast(msg, bad) {
    const t = h('div', { class: 'toast' + (bad ? ' bad' : ''), role: 'status' }, msg);
    document.body.append(t);
    setTimeout(() => t.remove(), bad ? 6000 : 3000);
  }
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
  async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); return null; } }
  window.X = { $, $$, h, s, api, fmtIn, money, toast, debounce, guard };
})();
