'use strict';
// Wiretap UI. Talks to the local server: /api/graph, /api/session, /api/source, /api/search, /api/stream (SSE).

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const trunc = (s, n) => (s = String(s ?? '')).length > n ? s.slice(0, n - 1) + '…' : s;
const plural = (n, w, ws) => `${n} ${n === 1 ? w : (ws || w + 's')}`;
const list = a => a.length <= 1 ? (a[0] || '') : a.slice(0, -1).join(', ') + ' and ' + a[a.length - 1];

const NW = 210, NH = 48, LW = 250, RH = 60, TOP = 50;
const CALLISH = new Set(['call', 'http', 'renders']);

const S = {
  g: null, I: null, r: { v: 'system' },
  sess: null, sessions: [], taskIdx: null, liveDot: false,
  trace: null, traceQ: '', summaries: {}, src: new Map(), sse: false,
};

// ======================= index =======================

function ownerOf(I, id) {
  if (id.startsWith('fn:')) return I.fns.get(id)?.file || null;
  if (id.startsWith('tg:')) return null;
  if (id.startsWith('rt:')) { const n = I.nodes.get(id); return n && n.missing ? id : null; }
  return id;
}

function buildIndex(g) {
  const I = { files: new Map(), fns: new Map(), nodes: new Map(), fnsByFile: new Map(), out: new Map(), inn: new Map(), apps: new Map(), appIdx: new Map() };
  g.apps.forEach((a, i) => { I.apps.set(a.id, a); I.appIdx.set(a.id, i % 6); });
  g.files.forEach(f => { I.files.set(f.id, f); I.fnsByFile.set(f.id, []); });
  g.fns.forEach(f => { I.fns.set(f.id, f); I.fnsByFile.get(f.file)?.push(f); });
  for (const a of I.fnsByFile.values()) a.sort((x, y) => x.line - y.line);
  g.nodes.forEach(n => I.nodes.set(n.id, n));
  const push = (m, k, v) => { let a = m.get(k); if (!a) m.set(k, a = []); a.push(v); };
  g.edges.forEach(e => { push(I.out, e.s, e); push(I.inn, e.t, e); });
  I.fout = new Map(); I.fin = new Map();
  const add = (m, a, b, e) => {
    let mm = m.get(a); if (!mm) m.set(a, mm = new Map());
    let r = mm.get(b); if (!r) mm.set(b, r = { n: 0, kinds: {}, labels: [], edges: [] });
    r.n++; r.kinds[e.k] = (r.kinds[e.k] || 0) + 1;
    if (e.l && r.labels.length < 6 && !r.labels.includes(e.l)) r.labels.push(e.l);
    r.edges.push(e);
  };
  g.edges.forEach(e => {
    if (e.k === 'trig') return;
    const a = ownerOf(I, e.s), b = ownerOf(I, e.t);
    if (!a || !b || a === b) return;
    add(I.fout, a, b, e); add(I.fin, b, a, e);
  });
  I.degree = id => (I.fout.get(id)?.size || 0) + (I.fin.get(id)?.size || 0);
  return I;
}

// ======================= node info =======================

const kindOf = id => id.startsWith('f:') ? 'file' : id.startsWith('fn:') ? 'fn' : id.startsWith('new:') ? 'new' : id.startsWith('more:') ? 'more' : (S.I.nodes.get(id)?.kind || '?');
function fileOfFn(id) { return S.I.fns.get(id)?.file; }
function appOf(id) {
  const k = kindOf(id);
  if (k === 'file') return S.I.files.get(id)?.app;
  if (k === 'fn') return S.I.files.get(fileOfFn(id))?.app;
  return S.I.nodes.get(id)?.app;
}
const appName = a => S.I.apps.get(a)?.name || a || '';
function fnShort(name) { const p = name.split('.'); return p.length > 2 ? p.slice(-2).join('.') : name; }
function labelOf(id) {
  const k = kindOf(id);
  if (k === 'file') return S.I.files.get(id)?.label || id;
  if (k === 'fn') return fnShort(S.I.fns.get(id)?.name || id) + (S.I.fns.get(id)?.kind === 'component' ? '' : '()');
  if (k === 'new') return id.slice(4).split('/').pop();
  return S.I.nodes.get(id)?.label || id;
}
function subOf(id) {
  const k = kindOf(id);
  if (k === 'file') { const f = S.I.files.get(id); const d = f.rel.split('/').slice(0, -1).join('/'); return trunc(d, 34) || '.'; }
  if (k === 'fn') return S.I.files.get(fileOfFn(id))?.label || '';
  if (k === 'new') return 'new file (planned)';
  const n = S.I.nodes.get(id) || {};
  if (n.kind === 'table') return n.rpc ? `${n.dbname} · function` : n.dbname;
  if (n.kind === 'ext') return 'Outside service';
  if (n.kind === 'resource') return appName(n.app) + ' · shared';
  if (n.kind === 'route') return n.missing ? 'No backend route matches' : appName(n.app);
  if (n.kind === 'trigger') return 'Where it starts';
  return n.kind || '';
}
function laneOf(id) {
  const k = kindOf(id);
  if (k === 'file') return S.I.files.get(id).role;
  if (k === 'new') return guessRole(id.slice(4));
  const n = S.I.nodes.get(id) || {};
  if (n.kind === 'table' || n.kind === 'resource') return 'data';
  if (n.kind === 'ext') return 'outside';
  if (n.kind === 'route') return 'route';
  return 'logic';
}
function guessRole(p) {
  p = p.toLowerCase();
  if (p.endsWith('.py')) return /router|endpoint/.test(p) ? 'route' : /model|schema/.test(p) ? 'data' : 'logic';
  if (/pages\/|screens\//.test(p)) return 'screen';
  if (/components\//.test(p)) return 'ui';
  return 'client';
}
function pathOf(id) {
  const k = kindOf(id);
  if (k === 'file') { const f = S.I.files.get(id); return `${appName(f.app)} · ${f.rel}`; }
  if (k === 'fn') { const fn = S.I.fns.get(id); const f = S.I.files.get(fn.file); return `${f.rel}:${fn.line}`; }
  return subOf(id);
}
function absPath(fileId) {
  const f = S.I.files.get(fileId); if (!f) return '';
  return S.I.apps.get(f.app).root + '/' + f.rel;
}
const appColor = id => `var(--app${S.I.appIdx.get(appOf(id)) ?? 0})`;

function edgeWords(r) {
  const k = r.kinds, parts = [];
  if (k.http) parts.push(r.labels.find(l => /^[A-Z]+ \//.test(l)) || 'API call');
  if (k.read && k.write) parts.push('reads & writes'); else if (k.read) parts.push('reads'); else if (k.write) parts.push('writes');
  if (k.db) parts.push('uses table');
  if (k.renders) parts.push('renders');
  if (k.call) parts.push(k.call > 1 ? `${k.call} calls` : 'calls');
  if (k.ext) parts.push('uses');
  if (k.res) parts.push('uses');
  const s = [...new Set(parts)].join(', ');
  return r.n > 1 && k.http > 1 ? s + ` +${k.http - 1}` : s;
}
function edgeWord(e) {
  return { http: e.l || 'API call', read: 'reads', write: 'writes', db: 'uses table', renders: 'renders', call: e.l ? e.l : 'calls', ext: 'uses', res: e.l || 'uses', trig: 'starts' }[e.k] || e.k;
}

// ======================= routing =======================

function encodeRoute(r) { return '#' + new URLSearchParams(Object.entries(r).filter(([, v]) => v != null && v !== '')).toString(); }
function parseRoute() {
  const p = new URLSearchParams(location.hash.slice(1));
  const r = Object.fromEntries(p.entries());
  if (!r.v) r.v = 'system';
  if (r.d) r.d = +r.d;
  return r;
}
function go(r) { const h = encodeRoute(r); if (location.hash === h) render(); else location.hash = h; }
window.addEventListener('hashchange', async () => {
  const prev = S.r?.sid;
  S.r = parseRoute();
  if (S.r.v === 'live' && S.r.sid && S.r.sid !== prev) await loadSession(S.r.sid);
  render();
});

// ======================= layout + drawing =======================

function layout(ids, edges, opts = {}) {
  const laneOrder = S.g.lanes.map(l => l.id);
  const byLane = new Map();
  for (const id of ids) { const l = opts.lane ? opts.lane(id) : laneOf(id); if (!byLane.has(l)) byLane.set(l, []); byLane.get(l).push(id); }
  const lanes = laneOrder.filter(l => byLane.has(l));
  for (const l of lanes) byLane.get(l).sort((a, b) => (opts.rank ? opts.rank(a) - opts.rank(b) : 0) || labelOf(a).localeCompare(labelOf(b)));
  const idx = new Map();
  const reindex = () => lanes.forEach(l => byLane.get(l).forEach((id, i) => idx.set(id, i)));
  reindex();
  const nbr = new Map();
  for (const [a, b] of edges) { (nbr.get(a) || nbr.set(a, []).get(a)).push(b); (nbr.get(b) || nbr.set(b, []).get(b)).push(a); }
  for (let it = 0; it < 6; it++) {
    for (const l of lanes) {
      const arr = byLane.get(l);
      const key = id => { const ns = (nbr.get(id) || []).filter(n => idx.has(n)); return ns.length ? ns.reduce((s, n) => s + idx.get(n), 0) / ns.length : idx.get(id); };
      const keys = new Map(arr.map(id => [id, opts.pin && opts.pin(id) ? -1 : key(id)]));
      arr.sort((a, b) => keys.get(a) - keys.get(b));
      arr.forEach((id, i) => idx.set(id, i));
    }
  }
  const maxRows = Math.max(1, ...lanes.map(l => byLane.get(l).length));
  const pos = new Map();
  lanes.forEach((l, li) => {
    const arr = byLane.get(l), off = (maxRows - arr.length) * RH / 2;
    arr.forEach((id, i) => pos.set(id, { x: 16 + li * LW, y: TOP + off + i * RH }));
  });
  return { pos, lanes: lanes.map((l, i) => ({ id: l, label: S.g.lanes.find(x => x.id === l).label, x: 16 + i * LW })), W: 16 + lanes.length * LW, H: TOP + maxRows * RH + 20 };
}

const MARKERS = ['line', 'accent', 'up', 'down', 'ghost', 'warn'];
const defs = () => '<defs>' + MARKERS.map(c => `<marker id="ar-${c}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" style="fill:var(--${c})"/></marker>`).join('') + '</defs>';
const markerFor = c => ({ hot: 'accent', up: 'up', down: 'down', ghost: 'ghost', new: 'down', warn: 'warn' }[c] || 'line');

function curve(A, B, w = NW, h = NH) {
  let x1, y1 = A.y + h / 2, x2, y2 = B.y + h / 2, d;
  if (B.x > A.x + 1) { x1 = A.x + w; x2 = B.x - 3; const c = Math.max(30, (x2 - x1) / 2); d = `M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2},${y2}`; }
  else if (B.x < A.x - 1) { x1 = A.x; x2 = B.x + w + 3; const c = Math.max(30, (x1 - x2) / 2); d = `M${x1},${y1} C${x1 - c},${y1} ${x2 + c},${y2} ${x2},${y2}`; }
  else { x1 = A.x + w; x2 = B.x + w + 3; const c = 36 + Math.abs(y2 - y1) / 6; d = `M${x1},${y1} C${x1 + c},${y1} ${x2 + c},${y2} ${x2},${y2}`; }
  return { d, mx: (x1 + x2) / 2 + (Math.abs(B.x - A.x) < 2 ? 34 : 0), my: (y1 + y2) / 2 };
}

function pillLabel(x, y, t) {
  t = trunc(t, 34);
  const w = t.length * 5.7 + 12;
  return `<g class="el" transform="translate(${x - w / 2},${y - 9})"><rect width="${w}" height="17" rx="8.5"/><text x="${w / 2}" y="12" text-anchor="middle">${esc(t)}</text></g>`;
}

// edges: [{a, b, cls, label, hit:[a,b]}], nodes: [{id, cls, badge, badgeColor, title, sub}]
function drawLanes(L, nodes, edges, opts = {}) {
  let s = defs();
  for (const l of L.lanes) {
    s += `<rect class="lane-bg" x="${l.x - 8}" y="8" width="${NW + 16}" height="${L.H - 16}" rx="8"/><text class="lane-t" x="${l.x}" y="30">${esc(l.label)}</text>`;
  }
  const labels = [];
  for (const e of edges) {
    const A = L.pos.get(e.a), B = L.pos.get(e.b);
    if (!A || !B) continue;
    const c = curve(A, B);
    if (e.hit) s += `<path class="hit" d="${c.d}" data-a="${esc(e.hit[0])}" data-b="${esc(e.hit[1])}"><title>See the calls behind this link</title></path>`;
    s += `<path class="edge ${e.cls || ''}" d="${c.d}" marker-end="url(#ar-${markerFor(e.cls)})"/>`;
    if (e.label) labels.push([c.mx, c.my, e.label]);
  }
  if (labels.length <= (opts.maxLabels ?? 18)) for (const [x, y, t] of labels) s += pillLabel(x, y, t);
  for (const n of nodes) {
    const p = L.pos.get(n.id); if (!p) continue;
    const title = n.title ?? labelOf(n.id), sub = n.sub ?? subOf(n.id);
    s += `<g class="node ${n.cls || ''}" tabindex="0" role="button" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})">
      <title>${esc(n.tip || pathOf(n.id))}</title>
      <rect class="box" width="${NW}" height="${NH}" rx="7"/>
      <rect class="stripe" x="0" y="8" width="3" height="${NH - 16}" rx="1.5" style="fill:${n.stripe || appColor(n.id)}"/>
      <text class="t" x="12" y="21">${esc(trunc(title, 27))}</text>
      <text class="s" x="12" y="37">${esc(trunc(sub, 32))}</text>
      ${n.badge ? `<circle r="10" cx="${NW - 4}" cy="4" style="fill:${n.badgeColor || 'var(--accent)'}"/><text class="badge-t" x="${NW - 4}" y="8" text-anchor="middle">${esc(n.badge)}</text>` : ''}
    </g>`;
  }
  return s;
}

function mountSvg(svgStr, W, H) {
  const svg = $('svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  svg.innerHTML = svgStr;
  svg.style.display = ''; $('empty').innerHTML = '';
}
function showEmpty(html) { const svg = $('svg'); svg.innerHTML = ''; svg.style.display = 'none'; $('empty').innerHTML = `<div class="empty">${html}</div>`; wireList($('empty')); }

function wireNodes(onClick, onDbl) {
  $('svg').querySelectorAll('.node').forEach(el => {
    el.addEventListener('click', () => onClick(el.dataset.id));
    if (onDbl) el.addEventListener('dblclick', () => onDbl(el.dataset.id));
    el.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onClick(el.dataset.id); } });
  });
  $('svg').querySelectorAll('.hit').forEach(el => {
    el.addEventListener('click', () => go({ v: 'link', a: el.dataset.a, b: el.dataset.b }));
    el.addEventListener('mouseenter', () => el.nextElementSibling?.classList.add('hov'));
    el.addEventListener('mouseleave', () => el.nextElementSibling?.classList.remove('hov'));
  });
}

function legend(items) { $('legend').innerHTML = items.map(([c, t]) => c ? `<span><i style="background:${c}"></i>${t}</span>` : `<span>${t}</span>`).join(''); }
function crumbs(parts) {
  $('crumbs').innerHTML = parts.map((p, i) => i === parts.length - 1 ? `<span class="cur">${esc(p[0])}</span>` : `<button data-i="${i}">${esc(p[0])}</button><span class="sep">›</span>`).join('');
  $('crumbs').querySelectorAll('button').forEach(b => b.addEventListener('click', () => go(parts[+b.dataset.i][1])));
}
function tools(html) { const t = $('tools'); t.hidden = !html; t.innerHTML = html || ''; }
const item = (pill, title, sub, file, attrs = '') => `<li ${attrs}><span class="pill ${pill[0]}">${esc(pill[1])}</span><span><span class="m">${title}</span>${sub ? `<span class="r">${sub}</span>` : ''}${file ? `<span class="f">${esc(file)}</span>` : ''}</span></li>`;
function wireList(root) {
  root.querySelectorAll('[data-go]').forEach(el => el.addEventListener('click', () => go(JSON.parse(el.dataset.go))));
}
const goAttr = r => `class="click" data-go="${esc(JSON.stringify(r))}"`;
function openRoute(id) {
  const k = kindOf(id);
  if (k === 'fn') return { v: 'file', id: fileOfFn(id), fn: id };
  if (k === 'trigger') return null;
  return { v: 'map', id };
}

// ======================= live overlay helpers =======================

function curTask() {
  const t = S.sess?.tasks || [];
  if (!t.length) return null;
  const i = S.taskIdx == null || S.taskIdx >= t.length ? t.length - 1 : S.taskIdx;
  return t[i];
}
function changedFnSet(task) {
  const m = new Map();
  if (!task) return m;
  for (const [fid, rec] of Object.entries(task.touched)) for (const [q, st] of Object.entries(rec.fns)) m.set(fid.replace('f:', 'fn:') + '#' + q, st);
  return m;
}

// ======================= views =======================

function render() {
  if (!S.g) return;
  document.querySelectorAll('#views button').forEach(b => b.setAttribute('aria-pressed', b.dataset.v === (['file', 'link'].includes(S.r.v) ? 'map' : S.r.v)));
  $('live-tab').innerHTML = 'Live' + (S.liveDot && S.r.v !== 'live' ? '<span class="dot"></span>' : '');
  if (S.r.v === 'live') S.liveDot = false;
  tools('');
  const v = { system: viewSystem, map: viewMap, file: viewFile, link: viewLink, live: viewLive, trace: viewTrace, resources: viewResources }[S.r.v] || viewSystem;
  try { v(); } catch (err) {
    console.error(err);
    $('panel').innerHTML = `<div class="callout"><b>This view hit a bug</b>${esc(err.message)}</div>`;
  }
}

// ---------- System ----------
function viewSystem() {
  const I = S.I, g = S.g;
  const cols = { client: 0, backend: 1, data: 2, outside: 3 };
  const X = [16, 330, 644, 958];
  const nodes = [];
  g.apps.forEach(a => nodes.push({ id: 'app:' + a.id, col: a.kind === 'client' ? 0 : 1, title: a.name, sub: `${(I.filesByApp?.get(a.id) || 0)} files`, app: a.id }));
  const dbs = new Map();
  for (const n of g.nodes) if (n.kind === 'table') { const d = dbs.get(n.db) || { id: 'db:' + n.db, title: n.dbname, n: 0 }; d.n++; dbs.set(n.db, d); }
  for (const d of dbs.values()) nodes.push({ id: d.id, col: 2, title: d.title, sub: plural(d.n, 'table') });
  const exts = g.nodes.filter(n => n.kind === 'ext');
  exts.forEach(n => nodes.push({ id: n.id, col: 3, title: n.label, sub: 'Outside service' }));
  const group = id => {
    const k = kindOf(id);
    if (k === 'fn' || k === 'file') return 'app:' + appOf(id);
    const n = I.nodes.get(id); if (!n) return null;
    if (n.kind === 'table') return 'db:' + n.db;
    if (n.kind === 'ext') return n.id;
    if (n.kind === 'route' && n.missing) return 'app:' + n.app;
    return null;
  };
  const agg = new Map();
  for (const e of g.edges) {
    if (e.k === 'trig' || e.k === 'res') continue;
    const a = group(e.s), b = group(e.t);
    if (!a || !b || a === b) continue;
    const key = a + '>' + b;
    const r = agg.get(key) || { a, b, n: 0, kinds: {} }; r.n++; r.kinds[e.k] = (r.kinds[e.k] || 0) + 1; agg.set(key, r);
  }
  const perCol = [0, 0, 0, 0];
  const colCount = [0, 0, 0, 0]; nodes.forEach(n => colCount[n.col]++);
  const maxRows = Math.max(...colCount);
  const pos = new Map();
  nodes.forEach(n => { const off = (maxRows - colCount[n.col]) * 70 / 2; pos.set(n.id, { x: X[n.col], y: TOP + off + perCol[n.col]++ * 70 }); });
  const W = X[3] + NW + 16, H = TOP + maxRows * 70 + 20;
  const sel = S.r.id;
  const touch = new Set(); if (sel) for (const r of agg.values()) if (r.a === sel || r.b === sel) { touch.add(r.a); touch.add(r.b); }
  let s = defs();
  ['Apps', 'Backends', 'Data', 'Outside services'].forEach((t, i) => { s += `<rect class="lane-bg" x="${X[i] - 8}" y="8" width="${NW + 16}" height="${H - 16}" rx="8"/><text class="lane-t" x="${X[i]}" y="30">${t}</text>`; });
  const labels = [];
  for (const r of agg.values()) {
    const A = pos.get(r.a), B = pos.get(r.b); if (!A || !B) continue;
    const on = sel && (r.a === sel || r.b === sel);
    const c = curve(A, B);
    const cls = sel ? (on ? 'hot' : 'faint') : '';
    s += `<path class="edge ${cls}" d="${c.d}" marker-end="url(#ar-${on ? 'accent' : 'line'})"/>`;
    if (!sel || on) {
      const k = r.kinds;
      const word = k.http ? `${k.http} API calls` : (k.read || k.write || k.db) ? `${(k.read || 0) + (k.write || 0) + (k.db || 0)} table uses` : plural(r.n, 'link');
      labels.push([c.mx, c.my, word]);
    }
  }
  if (labels.length < 30) labels.forEach(([x, y, t]) => s += pillLabel(x, y, t));
  for (const n of nodes) {
    const p = pos.get(n.id);
    const cls = sel ? (n.id === sel ? 'focus' : touch.has(n.id) ? 'lens' : 'dim') : '';
    const stripe = n.app ? `var(--app${I.appIdx.get(n.app)})` : n.col === 2 ? 'var(--ghost)' : 'var(--muted)';
    s += `<g class="node ${cls}" tabindex="0" role="button" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})"><rect class="box" width="${NW}" height="${NH}" rx="7"/><rect class="stripe" x="0" y="8" width="3" height="${NH - 16}" style="fill:${stripe}"/><text class="t" x="12" y="21">${esc(trunc(n.title, 27))}</text><text class="s" x="12" y="37">${esc(n.sub)}</text></g>`;
  }
  mountSvg(s, W, H);
  wireNodes(id => go({ v: 'system', id: S.r.id === id ? undefined : id }), id => go({ v: 'map', id }));
  crumbs([['System']]);
  const st = g.stats;
  const httpN = g.edges.filter(e => e.k === 'http').length, missing = g.nodes.filter(n => n.kind === 'route' && n.missing).length;
  $('readout').innerHTML = `<b>${esc(g.name)}</b>: ${plural(g.apps.length, 'app')}, ${st.files} files, ${st.functions} functions.
    <span class="k-acc">${httpN} API calls</span> from the apps reach their backends${missing ? `; <span class="k-warn">${missing} call${missing === 1 ? '' : 's'} hit no backend route</span>` : ''}.
    <small>Click a box to see what it talks to, then pick a file from the list on the right. Double-click opens it on the map. Press / to search.</small>`;
  legend([['var(--accent)', 'Selected and what it talks to'], [null, 'Numbers on lines count links in the code']]);
  // panel
  const P = $('panel');
  if (!sel) {
    P.innerHTML = `<div><h2>${esc(g.name)}</h2><p>Indexed ${new Date(g.generated * 1000).toLocaleTimeString()} in ${g.timings?.scan ?? '?'} s + ${g.timings?.link ?? '?'} s. Updates by itself when files change.</p></div>
      <div class="stats"><div class="stat acc"><b>${st.files}</b><span>files</span></div><div class="stat acc"><b>${st.functions}</b><span>functions</span></div><div class="stat"><b>${st.edges}</b><span>links</span></div></div>
      <div><h3>Apps</h3><ul class="list">${g.apps.map(a => item(['acc', a.kind], esc(a.name), `${I.filesByApp.get(a.id) || 0} files`, null, goAttr({ v: 'system', id: 'app:' + a.id }))).join('')}</ul></div>
      ${diagnostics()}`;
  } else if (sel.startsWith('app:')) {
    const aid = sel.slice(4), a = I.apps.get(aid);
    const files = g.files.filter(f => f.app === aid).sort((x, y) => I.degree(y.id) - I.degree(x.id));
    const routes = g.nodes.filter(n => n.kind === 'route' && n.app === aid && !n.missing).length;
    P.innerHTML = `<div><h2>${esc(a.name)}</h2><div class="path">${esc(a.root)}</div></div>
      <p>Click a file below to open it on the map.</p>
      <div class="stats"><div class="stat acc"><b>${files.length}</b><span>files</span></div><div class="stat acc"><b>${g.fns.filter(f => f.file.startsWith('f:' + aid + ':')).length}</b><span>functions</span></div><div class="stat"><b>${routes}</b><span>API routes</span></div></div>
      <div><h3>Most connected files</h3><ul class="list">${files.slice(0, 40).map(f => item(['acc', I.degree(f.id)], esc(f.label), null, f.rel, goAttr({ v: 'map', id: f.id }))).join('')}</ul></div>`;
  } else if (sel.startsWith('db:')) {
    const db = sel.slice(3);
    const tables = g.nodes.filter(n => n.kind === 'table' && n.db === db).map(n => ({ n, users: (I.inn.get(n.id) || []).length })).sort((a, b) => b.users - a.users);
    P.innerHTML = `<div><h2>${esc(tables[0]?.n.dbname || db)}</h2><p>${plural(tables.length, 'table')} used in code. Sorted by how many places touch them.</p></div>
      <ul class="list">${tables.map(t => item(['acc', t.users], esc(t.n.label), null, null, goAttr({ v: 'map', id: t.n.id }))).join('')}</ul>`;
  } else {
    P.innerHTML = `<div><h2>${esc(labelOf(sel))}</h2><p>${esc(subOf(sel))}</p></div><button class="btn" id="open">Open on the map →</button>`;
    $('open').onclick = () => go({ v: 'map', id: sel });
  }
  wireList(P);
}

function diagnostics() {
  const g = S.g;
  const um = g.unmatched_http || [];
  const unr = g.unresolved || [];
  return `<div><h3>How complete the map is</h3><ul class="list">
    ${item([um.length ? 'risk' : 'down', um.length ? um.length : 'ok'], 'API calls with no matching route', um.length ? 'Usually an old endpoint or a URL built at runtime.' : 'Every API call in the apps matched a backend route.')}
    ${item(['new', g.stats.unresolved_calls || 0], 'Calls the map could not follow', unr.slice(0, 4).map(([n, c]) => `${esc(n)} ×${c}`).join(' · '))}
  </ul></div>${um.length ? `<div><h3>Unmatched API calls</h3><ul class="list">${um.slice(0, 30).map(u => item(['risk', 'call'], esc(u.call), null, `${appName(u.app)} · ${u.file}:${u.line}`, goAttr({ v: 'file', id: `f:${u.app}:${u.file}` }))).join('')}</ul></div>` : ''}`;
}

// ---------- Map (focus) ----------
function bfs(start, dir, depth, cap) {
  const I = S.I, m = dir === 'up' ? I.fin : I.fout;
  const dist = new Map([[start, 0]]), more = new Map();
  let frontier = [start];
  for (let d = 1; d <= depth; d++) {
    const cand = new Map();
    for (const u of frontier) for (const [v, r] of (m.get(u) || new Map())) if (!dist.has(v)) cand.set(v, (cand.get(v) || 0) + r.n);
    const sorted = [...cand.entries()].sort((a, b) => b[1] - a[1]);
    const keep = sorted.slice(0, d === 1 ? cap : Math.ceil(cap / 2));
    if (sorted.length > keep.length) more.set(d, sorted.slice(keep.length).map(x => x[0]));
    frontier = keep.map(x => x[0]);
    frontier.forEach(v => dist.set(v, d));
  }
  dist.delete(start);
  return { dist, more };
}

function viewMap() {
  const I = S.I;
  let id = S.r.id;
  // an app or database picked in the System view: start from its most connected file or table
  if (id && id.startsWith('app:')) {
    const best = S.g.files.filter(f => f.app === id.slice(4)).sort((a, b) => I.degree(b.id) - I.degree(a.id))[0];
    if (best) { go({ v: 'map', id: best.id }); return; }
  }
  if (id && id.startsWith('db:')) {
    const best = S.g.nodes.filter(n => n.kind === 'table' && n.db === id.slice(3)).sort((a, b) => I.degree(b.id) - I.degree(a.id))[0];
    if (best) { go({ v: 'map', id: best.id }); return; }
  }
  if (!id || !(I.files.has(id) || I.nodes.has(id))) {
    const starts = S.g.files.slice().sort((a, b) => I.degree(b.id) - I.degree(a.id)).slice(0, 12);
    const picks = `<ul class="list">${starts.map(f => item(['acc', appName(f.app).split(' ')[0]], esc(f.label), null, f.rel, goAttr({ v: 'map', id: f.id }))).join('')}</ul>`;
    showEmpty(`<h2>Pick something to look at</h2><p>Press <b>/</b> and type a file, function, route or table name, or start from one of the most connected files:</p><div style="margin-top:12px">${picks}</div>`);
    crumbs([['System', { v: 'system' }], ['Map']]);
    $('readout').innerHTML = 'Nothing selected yet.'; legend([]);
    $('panel').innerHTML = `<div><h2>How to pick</h2><p>Search with <b>/</b>. In System, click an app and choose a file from the list on the right. Anywhere else, click a box.</p></div>`;
    return;
  }
  if (I.nodes.get(id)?.kind === 'route' && !I.nodes.get(id).missing) id = I.nodes.get(id).file;
  const depth = S.r.d || 2;
  const all = S.r.all === '1';
  const up = bfs(id, 'up', depth, 14), down = bfs(id, 'down', depth, 14);
  const ids = new Set([id, ...up.dist.keys(), ...down.dist.keys()]);
  const edges = [];
  const seen = new Set();
  const addE = (a, b, cls, label) => { const k = a + '>' + b; if (seen.has(k)) return; seen.add(k); edges.push({ a, b, cls, label, hit: [a, b] }); };
  for (const [u, du] of up.dist) for (const [v, r] of (I.fout.get(u) || new Map())) {
    const dv = v === id ? 0 : up.dist.get(v);
    if (dv !== undefined && dv === du - 1) addE(u, v, 'up', v === id ? edgeWords(r) : null);
  }
  for (const u of [id, ...down.dist.keys()]) {
    const du = u === id ? 0 : down.dist.get(u);
    for (const [v, r] of (I.fout.get(u) || new Map())) if (down.dist.get(v) === du + 1) addE(u, v, 'down', u === id ? edgeWords(r) : null);
  }
  if (all) for (const u of ids) for (const [v] of (I.fout.get(u) || new Map())) if (ids.has(v)) addE(u, v, 'faint', null);
  const L = layout([...ids], edges.map(e => [e.a, e.b]), { pin: x => x === id, rank: x => x === id ? -1 : (up.dist.get(x) ?? down.dist.get(x) ?? 0) });
  const live = liveClasses();
  const nodes = [...ids].map(n => ({ id: n, cls: (n === id ? 'focus' : up.dist.has(n) ? 'up' : 'down') + (I.nodes.get(n)?.missing ? ' missing' : '') + (live.cls.get(n) ? ' ' + live.cls.get(n) : ''), badge: live.badge.get(n), badgeColor: live.badgeColor.get(n) }));
  mountSvg(drawLanes(L, nodes, edges), L.W, L.H);
  wireNodes(n => {
    if (n === id) { if (kindOf(n) === 'file') go({ v: 'file', id: n }); return; }
    go({ v: 'map', id: n, d: S.r.d, all: S.r.all });
  });
  tools(`<label>Depth <select id="depth">${[1, 2, 3].map(d => `<option ${d === depth ? 'selected' : ''}>${d}</option>`).join('')}</select></label>
    <label><input type="checkbox" id="allx" ${all ? 'checked' : ''}> Show links between the others too</label>
    <span>Click a box to move there. Click the focused box again to look inside. Click a line to see the calls behind it.</span>`);
  $('depth').onchange = e => go({ ...S.r, id: S.r.id, d: +e.target.value });
  $('allx').onchange = e => go({ ...S.r, all: e.target.checked ? '1' : '' });

  const dUp = [...(I.fin.get(id) || new Map()).entries()].sort((a, b) => b[1].n - a[1].n);
  const dDown = [...(I.fout.get(id) || new Map()).entries()].sort((a, b) => b[1].n - a[1].n);
  const upApps = [...new Set([...up.dist.keys()].map(appOf).filter(Boolean).map(appName))];
  crumbs([['System', { v: 'system' }], [labelOf(id)]]);
  const kind = kindOf(id);
  $('readout').innerHTML = `<b>${esc(labelOf(id))}</b> is used by <span class="k-up">${plural(dUp.length, 'file')}</span>${upApps.length ? ` across ${esc(list(upApps))}` : ''} and directly reaches <span class="k-down">${dDown.length}</span>.
    <small>${dUp.length ? `If you change it, recheck: ${esc(list(dUp.slice(0, 5).map(([n]) => labelOf(n))))}${dUp.length > 5 ? ` and ${dUp.length - 5} more` : ''}.` : 'Nothing calls into it from another file.'}${up.more.size || down.more.size ? ' Some boxes were left out to keep the map readable; the side panel lists everything.' : ''}</small>`;
  legend([['var(--accent)', 'Focus'], ['var(--up)', 'Uses it'], ['var(--down)', 'It reaches'], ['var(--warn)', 'Missing backend route']]);

  const P = $('panel');
  const n = I.nodes.get(id);
  const fileInfo = kind === 'file' ? `<div class="row-btns"><button class="btn" id="inside">Look inside →</button><button class="btn2" id="copy">Copy path</button></div>` : '';
  let extra = '';
  if (n?.kind === 'table' || n?.kind === 'resource' || n?.kind === 'ext') {
    const users = (I.inn.get(id) || []).filter(e => e.s.startsWith('fn:'));
    const byKind = k => users.filter(e => e.k === k).length;
    extra = `<div class="stats"><div class="stat acc"><b>${users.length}</b><span>functions use it</span></div>${n.kind === 'table' ? `<div class="stat down"><b>${byKind('read')}</b><span>read</span></div><div class="stat up"><b>${byKind('write')}</b><span>write</span></div>` : ''}</div>
      <div><h3>Functions that use it</h3><ul class="list">${users.slice(0, 80).map(e => item([e.k === 'write' ? 'up' : 'down', edgeWord(e)], esc(labelOf(e.s)), e.l && e.k !== 'res' ? esc(e.l) : null, pathOf(e.s), goAttr(openRoute(e.s)))).join('')}</ul></div>`;
    if (n.desc) extra = `<p>${esc(n.desc)}</p>` + extra;
  }
  P.innerHTML = `<div><h2>${esc(labelOf(id))}</h2><div class="path">${esc(pathOf(id))}</div></div>${fileInfo}${extra}
    <div><h3>Directly used by</h3>${dUp.length ? `<ul class="list">${dUp.slice(0, 60).map(([o, r]) => item(['up', edgeWords(r) || 'uses'], esc(labelOf(o)), null, pathOf(o), goAttr({ v: 'map', id: o, d: S.r.d }))).join('')}</ul>` : '<p>Nothing in another file calls it.</p>'}</div>
    <div><h3>Directly reaches</h3>${dDown.length ? `<ul class="list">${dDown.slice(0, 60).map(([o, r]) => item(['down', edgeWords(r) || 'uses'], esc(labelOf(o)), null, pathOf(o), goAttr({ v: 'map', id: o, d: S.r.d }))).join('')}</ul>` : '<p>It calls nothing in other files.</p>'}</div>`;
  if (kind === 'file') {
    $('inside').onclick = () => go({ v: 'file', id });
    $('copy').onclick = () => copyText(absPath(id), $('copy'));
  }
  wireList(P);
}

function copyText(t, btn) {
  const done = () => { const o = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = o, 1200); };
  navigator.clipboard?.writeText(t).then(done).catch(() => { prompt('Copy:', t); });
}

// ---------- File (functions) ----------
function fnEdgesIn(fid) { return (S.I.inn.get(fid) || []).filter(e => CALLISH.has(e.k) || e.k === 'trig'); }
function fnEdgesOut(fid) { return (S.I.out.get(fid) || []).filter(e => e.k !== 'trig'); }

function chainBack(fid) {
  const I = S.I, steps = [{ id: fid }], seen = new Set([fid]);
  let cur = fid;
  for (let i = 0; i < 12; i++) {
    const ins = fnEdgesIn(cur);
    const pick = ins.find(e => e.s.startsWith('tg:'))
      || ins.find(e => e.k === 'http' && !seen.has(e.s))
      || ins.find(e => e.k !== 'trig' && fileOfFn(e.s) === fileOfFn(cur) && !seen.has(e.s))
      || ins.find(e => e.k !== 'trig' && e.s.startsWith('fn:') && !seen.has(e.s))
      || ins.find(e => e.s.startsWith('rt:'));
    if (!pick) break;
    if (pick.s.startsWith('tg:') || pick.s.startsWith('rt:')) { steps.push({ id: pick.s, start: true }); break; }
    seen.add(pick.s);
    steps.push({ id: pick.s, via: pick.k === 'http' ? pick.l : null });
    cur = pick.s;
  }
  return steps.reverse();
}
function upstreamFns(ids, depth = 3) {
  const seen = new Set(ids); let frontier = [...ids];
  for (let d = 0; d < depth; d++) {
    const nxt = [];
    for (const f of frontier) for (const e of (S.I.inn.get(f) || [])) if (CALLISH.has(e.k) && !seen.has(e.s)) { seen.add(e.s); nxt.push(e.s); }
    frontier = nxt;
  }
  ids.forEach(i => seen.delete(i));
  return seen;
}

function row(x, y, w, t, s, cls, attrs) {
  return `<g class="row ${cls || ''}" tabindex="0" role="button" ${attrs} transform="translate(${x},${y})"><rect width="${w}" height="40" rx="6"/><text class="t" x="10" y="17">${esc(trunc(t, Math.floor(w / 7.3)))}</text><text class="s" x="10" y="31">${esc(trunc(s, Math.floor(w / 5.6)))}</text></g>`;
}
function link(x1, y1, x2, y2, cls, label, show) {
  const c = Math.max(30, (x2 - x1) / 2);
  let s = `<path class="edge ${cls}" d="M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2 - 3},${y2}" marker-end="url(#ar-${markerFor(cls)})"/>`;
  if (show && label) s += pillLabel((x1 + x2) / 2, (y1 + y2) / 2, label);
  return s;
}

function viewFile() {
  const I = S.I, fileId = S.r.id, f = I.files.get(fileId);
  if (!f) { go({ v: 'system' }); return; }
  const fns = I.fnsByFile.get(fileId) || [];
  const sel = S.r.fn && I.fns.has(S.r.fn) ? S.r.fn : null;
  const changed = changedFnSet(curTask());
  const LX = 16, LW2 = 270, MX = 400, MW = 290, RX = 820, RW = 270, TOPY = 56, R = 50;
  let s = defs();
  let left = [], right = [], mid = fns, lines = [];
  const inside = id => fileOfFn(id) === fileId;
  if (sel) {
    const ins = fnEdgesIn(sel), outs = fnEdgesOut(sel);
    left = [...new Map(ins.map(e => [e.s, e])).values()];
    right = [...new Map(outs.map(e => [e.t, e])).values()];
    lines = [...left.map(e => ({ side: 'L', other: e.s, fn: sel, e })), ...right.map(e => ({ side: 'R', other: e.t, fn: sel, e }))];
  } else {
    const lm = new Map(), rm = new Map();
    for (const fn of fns) {
      for (const e of fnEdgesIn(fn.id)) {
        if (inside(e.s)) continue;
        const o = e.s.startsWith('fn:') ? fileOfFn(e.s) : e.s;
        if (!lm.has(o)) lm.set(o, { id: o, n: 0, fns: new Set() }); lm.get(o).n++; lm.get(o).fns.add(fn.id);
        lines.push({ side: 'L', other: o, fn: fn.id, e });
      }
      for (const e of fnEdgesOut(fn.id)) {
        if (inside(e.t)) continue;
        const o = e.t.startsWith('fn:') ? fileOfFn(e.t) : e.t;
        if (!rm.has(o)) rm.set(o, { id: o, n: 0 }); rm.get(o).n++;
        lines.push({ side: 'R', other: o, fn: fn.id, e });
      }
    }
    left = [...lm.values()].sort((a, b) => b.n - a.n).slice(0, 40);
    right = [...rm.values()].sort((a, b) => b.n - a.n).slice(0, 40);
  }
  const midIdx = new Map(mid.map((fn, i) => [fn.id, i]));
  const sideKey = x => sel ? (x.side === 'L' ? x.e.s : x.e.t) : x.other;
  const leftIds = left.map(x => sel ? x.s : x.id), rightIds = right.map(x => sel ? x.t : x.id);
  const rows = Math.max(mid.length, left.length, right.length, 1);
  const H = TOPY + 40 + rows * R + 30, W = RX + RW + 16;
  const midY = i => TOPY + 40 + i * R;
  const sideY = i => TOPY + 40 + i * R;
  s += `<text class="lane-t" x="${LX}" y="30">${sel ? 'Called from' : 'Files that call in'}</text><text class="lane-t" x="${MX}" y="30">Inside this file</text><text class="lane-t" x="${RX}" y="30">${sel ? 'Calls out to' : 'Reaches'}</text>`;
  s += `<rect class="cont" x="${MX - 10}" y="${TOPY - 6}" width="${MW + 20}" height="${mid.length * R + 46}" rx="10"/><text class="cont-t" x="${MX}" y="${TOPY + 14}">${esc(f.label)}</text><text class="cont-s" x="${MX}" y="${TOPY + 28}">${esc(trunc(appName(f.app) + ' · ' + f.rel, 48))}</text>`;
  const drawLines = sel || lines.length <= 120;
  const quiet = w => w === 'calls' && lines.length > 8;  // plain "calls" labels only when few, to avoid a pile-up
  if (drawLines) {
    for (const ln of lines) {
      const mi = midIdx.get(ln.fn); if (mi === undefined) continue;
      if (ln.side === 'L') {
        const li = leftIds.indexOf(sideKey(ln)); if (li < 0) continue;
        s += link(LX + LW2, sideY(li) + 20, MX, midY(mi) + 20, sel ? 'hot' : '', sel ? edgeWord(ln.e) : null, !!sel && !quiet(edgeWord(ln.e)));
      } else {
        const ri = rightIds.indexOf(sideKey(ln)); if (ri < 0) continue;
        s += link(MX + MW, midY(mi) + 20, RX, sideY(ri) + 20, sel ? (ln.e.k === 'write' ? 'up' : 'hot') : '', sel ? edgeWord(ln.e) : null, !!sel && !quiet(edgeWord(ln.e)));
      }
    }
  }
  const sideRow = (x, i, w, id, extraSub) => {
    const k = kindOf(id);
    if (k === 'trigger') { const n = I.nodes.get(id); return row(x, sideY(i), w, n.label, 'where the flow starts', 'trig', ''); }
    if (k === 'route') { const n = I.nodes.get(id); return row(x, sideY(i), w, n.label, n.missing ? 'no backend route' : 'API route (entry)', 'trig', ''); }
    if (k === 'fn') { const same = inside(id); return row(x, sideY(i), w, labelOf(id), same ? 'same file' : `${labelOf(fileOfFn(id))}${extraSub || ''}`, changed.has(id) ? 'chg' : '', same ? `data-self="${esc(id)}"` : `data-go="${esc(JSON.stringify(openRoute(id)))}"`); }
    return row(x, sideY(i), w, labelOf(id), subOf(id) + (extraSub || ''), 'file', `data-go="${esc(JSON.stringify({ v: k === 'file' ? 'file' : 'map', id }))}"`);
  };
  left.forEach((x, i) => s += sideRow(LX, i, LW2, sel ? x.s : x.id, sel ? '' : ` · ${x.n} call${x.n === 1 ? '' : 's'}`));
  right.forEach((x, i) => s += sideRow(RX, i, RW, sel ? x.t : x.id, sel ? '' : ` · ${x.n}`));
  mid.forEach((fn, i) => {
    const nin = fnEdgesIn(fn.id).length, nout = fnEdgesOut(fn.id).length;
    const ch = changed.get(fn.id);
    const sub = ch ? `${ch} by Claude · line ${fn.line}` : `line ${fn.line} · ${nin} in · ${nout} out`;
    s += row(MX, midY(i), MW, fnShort(fn.name) + (fn.kind === 'component' ? '' : '()'), sub, (sel === fn.id ? 'on' : sel ? 'dim' : '') + (ch ? (ch === 'added' ? ' added' : ' chg') : ''), `data-self="${esc(fn.id)}"`);
  });
  if (!fns.length) s += `<text class="cont-s" x="${MX}" y="${TOPY + 60}">No functions found in this file.</text>`;
  mountSvg(s, W, H);
  $('svg').querySelectorAll('.row').forEach(el => {
    const goRow = () => {
      if (el.dataset.self) go({ v: 'file', id: fileId, fn: S.r.fn === el.dataset.self ? undefined : el.dataset.self });
      else if (el.dataset.go) go(JSON.parse(el.dataset.go));
    };
    el.addEventListener('click', goRow);
    el.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); goRow(); } });
  });

  crumbs([['System', { v: 'system' }], [f.label, { v: 'map', id: fileId }], ...(sel ? [[labelOf(sel)]] : [['Inside']])]);
  legend([['var(--accent)', 'Selected function / call'], ['var(--down)', 'Where a flow starts'], [null, 'Click a function to trace it; click a box on either side to step into that file']]);
  const P = $('panel');
  if (!sel) {
    const nIn = new Set(left.map(x => x.id)).size, nOut = right.length;
    $('readout').innerHTML = `<b>${esc(f.label)}</b> has ${plural(fns.length, 'function')}. <span class="k-up">${plural(nIn, 'file')}</span> call into it; it reaches <span class="k-down">${nOut}</span>.
      <small>Click a function to see its calls and trace it back to where the flow starts.${!drawLines ? ' Lines are hidden because there are too many; pick a function.' : ''}</small>`;
    P.innerHTML = `<div><h2>${esc(f.label)}</h2><div class="path">${esc(appName(f.app) + ' · ' + f.rel)}</div></div>
      <div class="row-btns"><button class="btn2" id="tomap">Show on map</button><button class="btn2" id="copy">Copy path</button></div>
      <div><h3>Functions</h3><ul class="list">${fns.map(fn => item([changed.get(fn.id) ? 'acc' : '', changed.get(fn.id) || ('line ' + fn.line)], esc(fnShort(fn.name)), `${fnEdgesIn(fn.id).length} in · ${fnEdgesOut(fn.id).length} out`, null, goAttr({ v: 'file', id: fileId, fn: fn.id }))).join('')}</ul></div>`;
    $('tomap').onclick = () => go({ v: 'map', id: fileId });
    $('copy').onclick = () => copyText(absPath(fileId), $('copy'));
    wireList(P);
    return;
  }
  const fn = I.fns.get(sel);
  const chain = chainBack(sel);
  const up = upstreamFns([sel]);
  const upFiles = new Set([...up].map(fileOfFn).filter(Boolean));
  const entryText = chain[0]?.start ? labelOf(chain[0].id) : null;
  $('readout').innerHTML = `<b class="code">${esc(fnShort(fn.name))}</b> has <span class="k-up">${plural(left.length, 'way')} in</span> and <span class="k-down">${right.length}</span> out.
    <small>${entryText ? `Starts at: ${chain.map(st => st.start ? `<span class="pill down">start</span> ${esc(labelOf(st.id))}` : `<span class="code">${esc(labelOf(st.id))}</span>${st.via ? ` <span class="pill acc">${esc(st.via)}</span>` : ''}`).join(' → ')}` : 'No caller found in the code. It may be called from outside (a framework, a test, or by name).'}</small>`;
  const ch = changed.get(sel);
  P.innerHTML = `<div><h2 class="code">${esc(fn.name)}</h2><div class="path">${esc(f.rel)}:${fn.line}–${fn.end}</div></div>
    ${ch ? `<div class="callout a"><b>${esc(ch)} by Claude in this session</b>See the Live view for the whole change.</div>` : ''}
    <div class="stats"><div class="stat up"><b>${left.length}</b><span>ways in</span></div><div class="stat down"><b>${right.length}</b><span>calls out</span></div><div class="stat acc"><b>${upFiles.size}</b><span>files upstream (3 hops)</span></div></div>
    <div><h3>Where it starts</h3>${chain.length > 1 || chain[0]?.start ? `<ul class="list">${chain.map(st => st.start ? item(['down', 'start'], esc(labelOf(st.id)), null, pathOf(I.nodes.get(st.id)?.file || st.id)) : item(['acc', st.via ? st.via.split(' ')[0] : 'call'], esc(labelOf(st.id)), st.via ? esc(st.via) : null, pathOf(st.id), goAttr(openRoute(st.id)))).join('')}</ul>` : '<p>No caller found in the code.</p>'}</div>
    <div><h3>Code</h3><pre class="src" id="src">Loading…</pre></div>
    <div class="row-btns"><button class="btn2" id="copy">Copy path:line</button></div>`;
  $('copy').onclick = () => copyText(absPath(fileId) + ':' + fn.line, $('copy'));
  wireList(P);
  loadSource(fileId, fn.line, Math.min(fn.end, fn.line + 150));
}

async function loadSource(fileId, start, end) {
  const key = `${fileId}:${start}:${end}:${S.g.version}`;
  let data = S.src.get(key);
  if (!data) {
    try { data = await (await fetch(`/api/source?file=${encodeURIComponent(fileId)}&start=${start}&end=${end}`)).json(); S.src.set(key, data); }
    catch { data = { lines: ['(could not load source)'], start }; }
  }
  const el = $('src'); if (!el) return;
  el.innerHTML = (data.lines || []).map((l, i) => `<span class="ln">${data.start + i}</span>${esc(l)}`).join('\n');
}

// ---------- Link ----------
function viewLink() {
  const I = S.I, a = S.r.a, b = S.r.b;
  const r = I.fout.get(a)?.get(b);
  if (!r) { go({ v: 'map', id: a }); return; }
  const srcFns = [...new Set(r.edges.map(e => e.s))], dstFns = [...new Set(r.edges.map(e => e.t))];
  const AX = 40, BX = 620, CW = 320, TOPY = 56, R = 50;
  const rows = Math.max(srcFns.length, dstFns.length);
  const W = BX + CW + 40, H = TOPY + 50 + rows * R + 20;
  const y = (i, n) => TOPY + 46 + i * R + (rows - n) * R / 2;
  let s = defs() + `<text class="lane-t" x="${AX}" y="30">From ${esc(labelOf(a))}</text><text class="lane-t" x="${BX}" y="30">To ${esc(labelOf(b))}</text>`;
  r.edges.forEach(e => {
    const i = srcFns.indexOf(e.s), j = dstFns.indexOf(e.t);
    s += link(AX + CW, y(i, srcFns.length) + 20, BX, y(j, dstFns.length) + 20, e.k === 'write' ? 'up' : 'hot', edgeWord(e), r.edges.length <= 24);
  });
  srcFns.forEach((id, i) => s += row(AX, y(i, srcFns.length), CW, labelOf(id), kindOf(id) === 'fn' ? 'line ' + I.fns.get(id).line : subOf(id), '', `data-go="${esc(JSON.stringify(openRoute(id)))}"`));
  dstFns.forEach((id, i) => s += row(BX, y(i, dstFns.length), CW, labelOf(id), kindOf(id) === 'fn' ? 'line ' + I.fns.get(id).line : subOf(id), '', `data-go="${esc(JSON.stringify(openRoute(id) || { v: 'map', id }))}"`));
  mountSvg(s, W, H);
  $('svg').querySelectorAll('.row').forEach(el => el.addEventListener('click', () => el.dataset.go && go(JSON.parse(el.dataset.go))));
  crumbs([['System', { v: 'system' }], [labelOf(a), { v: 'map', id: a }], [`→ ${labelOf(b)}`]]);
  $('readout').innerHTML = `<b>${esc(labelOf(a))}</b> talks to <b>${esc(labelOf(b))}</b> through <span class="k-down">${plural(r.edges.length, 'call')}</span>.<small>${esc(edgeWords(r))}</small>`;
  legend([['var(--accent)', 'Call'], ['var(--up)', 'Write']]);
  $('panel').innerHTML = `<div><h2>The calls behind this link</h2></div><ul class="list">${r.edges.map(e => item([e.k === 'write' ? 'up' : 'acc', e.k], `${esc(labelOf(e.s))} → ${esc(labelOf(e.t))}`, e.l ? esc(e.l) : null, `${pathOf(e.s)}${e.line ? ' (line ' + e.line + ')' : ''}`, goAttr(openRoute(e.s)))).join('')}</ul>`;
  wireList($('panel'));
}

// ---------- Live ----------
function liveClasses() {
  const out = { cls: new Map(), badge: new Map(), badgeColor: new Map() };
  const t = curTask(); if (!t) return out;
  const ripple = new Set(t.ripple_files || []);
  for (const f of ripple) out.cls.set(f, 'up');
  for (const f of t.planned || []) if (!t.touched[f]) out.cls.set(f, 'ghost');
  for (const [f, rec] of Object.entries(t.touched)) {
    const off = (t.summary.off_plan || []).includes(f);
    out.cls.set(f, rec.status === 'created' ? 'new' : off ? 'warn' : 'edit');
    const n = Object.keys(rec.fns).length;
    out.badge.set(f, off ? '!' : n ? String(n) : null);
    out.badgeColor.set(f, off ? 'var(--warn)' : rec.status === 'created' ? 'var(--down)' : 'var(--accent)');
  }
  const last = [...t.events].reverse().find(e => e.file);
  if (last) out.cls.set(last.file, (out.cls.get(last.file) || 'read') + ' cur');
  return out;
}

function evText(e) {
  const f = e.file ? labelOf(e.file) : (e.path ? e.path.split('/').pop() : '');
  const fn = (e.fns || []).map(x => x.name.split('.').pop()).slice(0, 3).join(', ');
  // read events carry function names, search events {file, names}; every template below is built for every event
  const seen = (e.seen || []).flatMap(x => typeof x === 'string' ? [x] : (x.names || [])).slice(0, 3).map(n => fnShort(n)).join(', ');
  return {
    prompt: [['acc', 'you'], `“${trunc(e.text, 140)}”`],
    read: [['', 'read'], `${f}${seen ? ' · ' + seen : ''}`],
    search: [['', 'search'], e.pattern
      ? `“${trunc(e.pattern, 60)}”${e.where ? ' in ' + trunc(e.where, 40) : ''} → ${e.n ? (e.n === 1 && e.files?.[0] ? labelOf(e.files[0]) : plural(e.n, 'file')) : 'nothing found'}${seen ? ' · ' + seen : ''}`
      : `“${trunc(e.text, 90)}”${e.n ? ` · found in ${plural(e.n, 'file')}` : ''}`],
    run: [['', 'shell'], trunc(e.text, 90)],
    plan: [['new', 'plan'], `Plan ready · ${plural((e.files || []).length, 'file')}${e.new?.length ? `, ${e.new.length} new` : ''}`],
    create: [['down', 'new'], `${f} (+${e.add || 0})`],
    edit: [[e.off ? 'risk' : 'acc', e.via === 'editor' ? 'you' : 'edit'], `${f}${e.add || e.dele ? ` (+${e.add || 0} −${e.dele || 0})` : ''}${fn ? ' · ' + fn : ''}${e.off ? ' · not in plan' : ''}${e.via === 'shell' ? ' · via shell' : ''}`],
    delete: [['risk', 'deleted'], f],
    test: [[e.failed ? 'risk' : 'down', 'test'], e.passed != null ? `${e.passed} passed, ${e.failed} failed` : trunc(e.text, 60)],
    stop: [['down', 'done'], 'Claude finished'],
  }[e.k] || [['', e.k], ''];
}

// ---------- Live: question mode ----------
// When Claude reads but changes nothing, Live shows "strands": one row per flow Claude
// followed, left to right from where it starts (a tap, a screen, a route) to the tables
// it touches. Rebuilt from the timeline, so Replay can stop at any step.
function lookedFrom(events, record) {
  const order = new Map(), score = new Map(), how = new Map();
  const credit = (file, names, h) => {
    names = [...new Set(names)];
    for (const n of names) {
      const id = file.replace('f:', 'fn:') + '#' + n;
      if (!S.I.fns.has(id)) continue;
      if (!order.has(id)) order.set(id, order.size + 1);
      score.set(id, (score.get(id) || 0) + 1 / names.length);
      if (h === 'read' || !how.has(id)) how.set(id, h);
    }
  };
  for (const e of events) {
    if (e.k === 'read' && e.file && e.seen?.length) credit(e.file, e.seen, 'read');
    // a search matching everywhere says nothing about where the answer is
    if (e.k === 'search' && e.seen && (e.n || 0) <= 6) for (const x of e.seen) if (x.names.length <= 5) credit(x.file, x.names, 'found');
  }
  // the server's record is complete (it also counts looks the timeline doesn't spell out)
  if (record) {
    score.clear();
    for (const x of record) for (const f of x.fns) {
      const id = x.file.replace('f:', 'fn:') + '#' + f.name;
      if (!S.I.fns.has(id)) continue;
      if (!order.has(id)) order.set(id, order.size + 1);
      score.set(id, f.score || 0); how.set(id, f.how);
    }
  }
  return { order, score, how };
}

function fwdPath(from, isGoal) {
  const prev = new Map([[from, null]]);
  let q = [from];
  for (let d = 0; d < 4 && q.length; d++) {
    const nx = [];
    for (const a of q) for (const e of fnEdgesOut(a)) {
      if (!e.t.startsWith('fn:') || prev.has(e.t)) continue;
      prev.set(e.t, a);
      if (isGoal(e.t)) { const p = []; for (let c = e.t; c !== from; c = prev.get(c)) p.unshift(c); return p; }
      nx.push(e.t);
    }
    q = nx;
  }
  return null;
}

function buildStrands(L) {
  const ranked = [...L.score.keys()].sort((a, b) => L.score.get(b) - L.score.get(a));
  // functions Claude actually read; ones a grep merely matched only count when nothing was read
  let focus = ranked.filter(id => L.how.get(id) === 'read');
  if (!focus.length) focus = ranked;
  const top = L.score.get(focus[0]) || 0;
  focus = focus.filter(id => L.score.get(id) >= top * 0.1).slice(0, 10);
  const isFocus = new Set(focus), assigned = new Set(), list = [];
  // longest chains first, so a function on another one's path joins that strand
  const chains = focus.map(f => ({ f, ch: chainBack(f) })).sort((a, b) => b.ch.length - a.ch.length);
  for (const { f, ch } of chains) {
    if (assigned.has(f)) continue;
    const nodes = ch.map(x => x.id), via = ch.map(x => x.via || null);
    let end = f;
    for (let hop = 0; hop < 4; hop++) {
      const path = fwdPath(end, id => isFocus.has(id) && !assigned.has(id) && !nodes.includes(id));
      if (!path) break;
      path.forEach(id => { nodes.push(id); via.push(null); });
      end = path[path.length - 1];
    }
    nodes.filter(n => isFocus.has(n)).forEach(n => assigned.add(n));
    const sinks = [];
    for (const n of nodes) if (isFocus.has(n)) for (const e of fnEdgesOut(n)) {
      if (/^(tb|ext):/.test(e.t) && !sinks.some(x => x.id === e.t)) sinks.push({ id: e.t, from: n, k: e.k });
    }
    const first = Math.min(...nodes.filter(n => L.order.has(n)).map(n => L.order.get(n)));
    list.push({ ...collapse(nodes, via, isFocus), sinks: sinks.slice(0, 3), first });
  }
  list.sort((a, b) => a.first - b.first);
  // a lone function with no known caller and no table isn't a flow: group those per file
  const loose = new Map();
  const flows = list.filter(st => {
    if (st.nodes.length > 1 || st.sinks.length) return true;
    const f = fileOfFn(st.nodes[0]);
    (loose.get(f) || loose.set(f, []).get(f)).push(st.nodes[0]);
    return false;
  });
  // flows with no known start that run between the same two files: one row ("a.py → b.py · 6 functions")
  const byPair = new Map();
  for (const st of flows) {
    if (/^(tg|rt):/.test(st.nodes[0]) || !st.nodes[0].startsWith('fn:')) continue;
    const last = [...st.nodes].reverse().find(n => isFocus.has(n));
    const k = fileOfFn(st.nodes[0]) + '>' + fileOfFn(last);
    (byPair.get(k) || byPair.set(k, []).get(k)).push(st);
  }
  const merged = new Set();
  for (const [k, sts] of byPair) {
    if (sts.length < 2) continue;
    const [fa, fb] = k.split('>');
    const grp = { pair: [fa, fb], callers: [...new Set(sts.map(x => x.nodes[0]))], fns: [...new Set(sts.flatMap(x => x.nodes.filter(n => isFocus.has(n))))],
      nodes: [], via: [], sinks: sts.flatMap(x => x.sinks).filter((x, i, a) => a.findIndex(y => y.id === x.id) === i).slice(0, 3), first: Math.min(...sts.map(x => x.first)) };
    sts.forEach(x => merged.add(x));
    flows.push(grp);
  }
  const out = flows.filter(x => !merged.has(x)).sort((a, b) => a.first - b.first);
  // a flow sharing its first steps with an earlier one starts where it branches off
  out.forEach((st, j) => {
    if (st.pair) return;
    let best = 0, from = null;
    for (const o of out.slice(0, j)) {
      if (o.pair || o.from) continue;
      let p = 0; while (p < st.nodes.length && p < o.nodes.length && st.nodes[p] === o.nodes[p]) p++;
      if (p > best) { best = p; from = o; }
    }
    if (best >= 2 && best < st.nodes.length) { st.from = st.nodes[best - 1]; st.nodes = st.nodes.slice(best - 1); st.via = st.via.slice(best - 1); }
  });
  // everything else Claude read stays visible as an "also read" card per file
  const inFlows = new Set(out.flatMap(st => st.pair ? [...st.callers, ...st.fns] : st.nodes));
  for (const id of ranked) {
    if (L.how.get(id) !== 'read' || inFlows.has(id) || [...loose.values()].some(v => v.includes(id))) continue;
    const f = fileOfFn(id);
    (loose.get(f) || loose.set(f, []).get(f)).push(id);
  }
  return { list: out, loose: [...loose].slice(0, 9).map(([file, fns]) => ({ file, fns })), isFocus };
}
// long chains: keep the start, routes, looked-at functions and their neighbours; fold the rest into "+N steps"
function collapse(nodes, via, isFocus) {
  const keep = nodes.map((n, i) => i === 0 || i === nodes.length - 1 || !n.startsWith('fn:') || isFocus.has(n) || isFocus.has(nodes[i - 1]) || isFocus.has(nodes[i + 1]) || via[i] || via[i - 1]);
  const outN = [], outV = [];
  for (let i = 0; i < nodes.length; i++) {
    if (keep[i]) { outN.push(nodes[i]); outV.push(via[i]); continue; }
    let j = i; while (j < nodes.length && !keep[j]) j++;
    if (j - i === 1) { outN.push(nodes[i]); outV.push(via[i]); continue; }
    outN.push('more:' + (j - i) + ':' + nodes[i]); outV.push(null);
    i = j - 1;
  }
  return { nodes: outN, via: outV };
}

// "Tap “Proceed” · Payment" instead of 'onClick on <button> “Proceed”'
function startLabel(id) {
  const n = S.I.nodes.get(id) || {};
  const screen = (S.I.files.get(n.file)?.label || '').replace(/\.(tsx|ts|jsx|js|py)$/, '');
  if (n.tkind === 'ui') { const m = /“(.*)”/.exec(n.label || ''); return [m ? `Tap “${m[1]}”` : (n.label || '').replace(/ on <.*/, ''), screen]; }
  if (n.tkind === 'screen') return [`${screen} opens`, 'screen'];
  return [n.label || labelOf(id), screen || subOf(id)];
}
function strandSentence(st) {
  if (st.pair) return `${labelOf(st.pair[0])} calls into ${labelOf(st.pair[1])} · ${plural(st.fns.length, 'function')} Claude looked at`;
  if (st.from) return `↳ branches off at ${labelOf(st.from)} → ` + strandSentence({ ...st, from: null, nodes: st.nodes.slice(1), via: st.via.slice(1) });
  const parts = st.nodes.map((n, i) => {
    if (n.startsWith('more:')) return '…';
    if (n.startsWith('tg:')) return startLabel(n).filter(Boolean).join(' on ');
    const v = st.via[i - 1];
    return (v ? v + ' → ' : '') + labelOf(n);
  });
  return parts.join(' → ') + (st.sinks.length ? ' → ' + st.sinks.map(x => `${x.k === 'write' ? 'writes' : 'reads'} ${labelOf(x.id)}`).join(', ') : '');
}

function drawStrands(list, L, isFocus, shown, loose = []) {
  const SW = 178, SH = 46, GAP = 46, HEAD = 30, PADY = 22, X0 = 20;
  const NUM = ['', '①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨'];
  let y = 14, s = defs(), W = 600;
  const avail = Math.max(900, ($('svg').parentNode?.clientWidth || 1100) - 20);
  const only = S.strandOnly;
  list.forEach((st, si) => {
    const dim = only != null && only !== si ? ' dim' : '';
    const rows = Math.max(1, st.sinks.length);
    s += `<g class="strand${dim}" data-strand="${si}"><text class="strand-h" x="${X0}" y="${y + 16}">${NUM[si + 1] || si + 1}  ${esc(trunc(strandSentence(st), Math.floor((avail - 40) / 6.6)))}</text>`;
    const ny = y + HEAD;
    if (st.pair) {
      const CW = 2 * SW + GAP, bx = X0 + CW + GAP, yy = ny + SH / 2;
      const card = (file, x, names, cls) => `<g class="node ${cls}" tabindex="0" role="button" data-id="${esc(file)}" transform="translate(${x},${ny})"><title>${esc(pathOf(file))}</title>
        <rect class="box" width="${CW}" height="${SH}" rx="7"/><rect class="stripe" x="0" y="8" width="3" height="${SH - 16}" rx="1.5" style="fill:${appColor(file)}"/>
        <text class="t" x="12" y="20">${esc(trunc(labelOf(file), 50))}</text><text class="s" x="12" y="35">${esc(trunc(names.join(', '), 72))}</text></g>`;
      s += `<path class="edge hot" d="M${X0 + CW},${yy} L${bx - 3},${yy}" marker-end="url(#ar-accent)"/>`;
      s += card(st.pair[0], X0, st.callers.map(n => fnShort(S.I.fns.get(n).name)), '');
      const order = st.fns.slice().sort((a, b) => (L.order.get(a) || 99) - (L.order.get(b) || 99));
      s += card(st.pair[1], bx, order.map(n => `${L.order.get(n) ? NUM[L.order.get(n)] || L.order.get(n) : ''}${fnShort(S.I.fns.get(n).name)}`), 'edit');
      st.sinks.forEach((k, j) => {
        const sx2 = bx + CW + GAP, ty = ny + j * (SH + 10);
        const c = curve({ x: bx + CW - SW, y: ny }, { x: sx2, y: ty }, SW, SH);
        s += `<path class="edge ${k.k === 'write' ? 'up' : 'down'}" d="${c.d}" marker-end="url(#ar-${k.k === 'write' ? 'up' : 'down'})"/>`;
        s += `<g class="node data" tabindex="0" role="button" data-id="${esc(k.id)}" transform="translate(${sx2},${ty})"><rect class="box" width="${SW}" height="${SH}" rx="7"/><text class="t" x="12" y="20">${esc(trunc(labelOf(k.id), 23))}</text><text class="s" x="12" y="35">${esc(k.k === 'write' ? 'written' : 'read')}</text></g>`;
        W = Math.max(W, sx2 + SW + 24);
      });
      s += '</g>';
      W = Math.max(W, bx + CW + 24);
      y = ny + Math.max(1, st.sinks.length) * (SH + 10) + PADY;
      return;
    }
    // wrap long flows to the canvas width instead of scrolling sideways
    const per = Math.max(3, Math.floor((avail - X0 - SW - 40) / (SW + GAP)));
    const pos = st.nodes.map((_, i) => ({ x: X0 + (i % per) * (SW + GAP), y: ny + Math.floor(i / per) * (SH + 26) }));
    const lines = Math.ceil(st.nodes.length / per);
    for (let i = 0; i < st.nodes.length - 1; i++) {
      const a = pos[i], b = pos[i + 1];
      if (b.y === a.y) { const yy = a.y + SH / 2; s += `<path class="edge hot" d="M${a.x + SW},${yy} L${b.x - 3},${yy}" marker-end="url(#ar-accent)"/>`; }
      else s += `<path class="edge hot" d="M${a.x + SW / 2},${a.y + SH} C${a.x + SW / 2},${a.y + SH + 18} ${b.x + SW / 2},${b.y - 18} ${b.x + SW / 2},${b.y - 3}" marker-end="url(#ar-accent)"/>`;
    }
    const lastP = pos[pos.length - 1];
    const sx = lastP.x + SW + GAP;
    st.sinks.forEach((k, j) => {
      const from = pos[Math.max(0, st.nodes.indexOf(k.from))] || lastP;
      const c = curve({ x: from.x, y: from.y }, { x: sx, y: lastP.y + j * (SH + 10) }, SW, SH);
      s += `<path class="edge ${k.k === 'write' ? 'up' : 'down'}" d="${c.d}" marker-end="url(#ar-${k.k === 'write' ? 'up' : 'down'})"/>`;
    });
    const node = (id, x, yy, cls, title, sub, badge) => {
      const appear = shown && !shown.has(si + '|' + id) ? ' appear' : '';
      return `<g class="node ${cls}${appear}" ${id.startsWith('more:') ? '' : `tabindex="0" role="button" data-id="${esc(id)}"`} transform="translate(${x},${yy})">
        <title>${esc(id.startsWith('more:') ? 'Steps in between, folded' : pathOf(id))}</title>
        <rect class="box" width="${SW}" height="${SH}" rx="7"/>
        <rect class="stripe" x="0" y="8" width="3" height="${SH - 16}" rx="1.5" style="fill:${id.startsWith('more:') ? 'var(--line)' : appColor(id)}"/>
        <text class="t" x="12" y="20">${esc(trunc(title, 23))}</text><text class="s" x="12" y="35">${esc(trunc(sub, 28))}</text>
        ${badge ? `<circle r="10" cx="${SW - 4}" cy="4" style="fill:var(--accent)"/><text class="badge-t" x="${SW - 4}" y="8" text-anchor="middle">${badge}</text>` : ''}</g>`;
    };
    st.nodes.forEach((id, i) => {
      const p = pos[i];
      if (id.startsWith('more:')) { s += node(id, p.x, p.y, 'more', `+${id.split(':')[1]} steps`, 'calls in between', null); return; }
      const [t, sub] = id.startsWith('tg:') ? startLabel(id) : [labelOf(id), (st.via[i - 1] ? st.via[i - 1] + ' · ' : '') + subOf(id)];
      const cls = id.startsWith('tg:') ? 'start' : isFocus.has(id) ? (L.how.get(id) === 'read' ? 'edit' : 'read') : L.order.has(id) ? 'read' : '';
      s += node(id, p.x, p.y, cls, t, sub, isFocus.has(id) ? L.order.get(id) : null);
    });
    st.sinks.forEach((k, j) => { s += node(k.id, sx, lastP.y + j * (SH + 10), 'data', labelOf(k.id), `${k.k === 'write' ? 'written' : 'read'} · ${subOf(k.id)}`, null); });
    s += '</g>';
    W = Math.max(W, ...pos.map(p => p.x + SW + 24), st.sinks.length ? sx + SW + 24 : 0);
    y = Math.max(lastP.y + rows * (SH + 10), ny + (lines - 1) * (SH + 26) + SH + 10) + PADY;
  });
  if (loose.length) {
    s += `<text class="strand-h loose" x="${X0}" y="${y + 16}">${list.length ? 'Also read (not on a flow above)' : 'Read'}</text>`;
    const CW = 2 * SW + GAP;
    loose.forEach((c, i) => {
      const x = X0 + (i % 3) * (CW + 16), yy = y + HEAD + Math.floor(i / 3) * (SH + 12);
      const names = c.fns.sort((a, b) => (L.order.get(a) || 99) - (L.order.get(b) || 99)).map(f => fnShort(S.I.fns.get(f).name));
      s += `<g class="node ${c.fns.some(f => L.how.get(f) === 'read') ? 'edit' : 'read'}" tabindex="0" role="button" data-id="${esc(c.file)}" transform="translate(${x},${yy})"><title>${esc(pathOf(c.file))}</title>
        <rect class="box" width="${CW}" height="${SH}" rx="7"/><rect class="stripe" x="0" y="8" width="3" height="${SH - 16}" rx="1.5" style="fill:${appColor(c.file)}"/>
        <text class="t" x="12" y="20">${esc(trunc(labelOf(c.file), 50))}</text><text class="s" x="12" y="35">${esc(trunc(names.join(', '), 70))}</text></g>`;
      W = Math.max(W, x + CW + 24);
    });
    y += HEAD + Math.ceil(loose.length / 3) * (SH + 12) + PADY;
  }
  return { svg: s, W, H: y };
}

function toggleReplay(t) {
  clearInterval(S.replayTimer);
  if (S.replay) { S.replay = null; render(); return; }
  S.replay = { key: S.sess.id + ':' + S.sess.tasks.indexOf(t), upto: 1 };
  S.replayTimer = setInterval(() => {
    if (!S.replay || S.r.v !== 'live') { clearInterval(S.replayTimer); S.replay = null; return; }
    if (++S.replay.upto > t.events.length) { clearInterval(S.replayTimer); S.replay = null; }
    render();
  }, 650);
  render();
}

function viewLive() {
  const I = S.I;
  crumbs([['Live session']]);
  legend([['var(--accent)', 'Edited (number = functions changed)'], ['var(--down)', 'Created'], ['var(--up)', 'Calls changed code'], ['var(--ghost)', 'In the plan, not touched yet'], ['var(--warn)', '! Edited outside the plan'], [null, 'Dashed = read or searched by Claude']]);
  const t = curTask();
  if (!S.sess || !t) {
    if (S.r.sid) {
      showEmpty(`<h2>Waiting for this Claude session</h2>
        <p>Opened from the Claude Code terminal. This page fills in as soon as Claude reads or edits a file in that session.</p>
        <p>If nothing appears after Claude has used a tool, the hooks are not installed yet:</p>
        <pre>cd ${esc(S.g.toolDir || 'wiretap')}\npython3 -m wiretap install-hooks</pre>
        <p><a href="#v=live">Show the latest session instead</a></p>`);
      $('readout').innerHTML = 'Waiting for Claude Code.';
      $('panel').innerHTML = `<div><h2>Live session</h2><p>Nothing yet.</p></div>`;
      return;
    }
    showEmpty(`<h2>No Claude Code session yet</h2>
      <p>Wiretap listens for Claude Code hooks. Install them once, then start Claude in your project. Files Claude reads, plans and edits will light up here while it works. Watching costs no tokens.</p>
      <pre>cd ${esc(S.g.toolDir || 'wiretap')}\npython3 -m wiretap install-hooks</pre>
      <p>Edits you make yourself still show up here, under “Changes made outside Claude”.</p>`);
    $('readout').innerHTML = 'Waiting for Claude Code.';
    $('panel').innerHTML = `<div><h2>Live session</h2><p>Nothing yet.</p></div>`;
    return;
  }
  const touched = Object.keys(t.touched);
  const ids = new Set([...touched.filter(f => I.files.has(f)), ...(t.planned || []).filter(f => I.files.has(f))]);
  const ripple = (t.ripple_files || []).filter(f => I.files.has(f)).sort((a, b) => I.degree(b) - I.degree(a));
  ripple.slice(0, 16).forEach(f => ids.add(f));
  // files Claude opened, plus the first few hits of each search (Bash grep counts too)
  const reads = [...t.events].reverse().flatMap(e => e.k === 'read' && e.file ? [e.file] : e.k === 'search' ? (e.files || []).slice(0, 3) : []).filter(f => I.files.has(f));
  [...new Set(reads)].slice(0, 8).forEach(f => ids.add(f));
  (t.planned_new || []).slice(0, 8).forEach(p => { if (!touched.some(f => f.endsWith(p))) ids.add('new:' + p); });
  const live = liveClasses();
  const edges = [];
  for (const a of ids) for (const [b, r] of (I.fout.get(a) || new Map())) if (ids.has(b)) {
    const hot = x => t.touched[x] || ripple.includes(x);
    const cls = t.touched[a]?.status === 'created' ? 'new' : (hot(a) && hot(b)) ? 'up' : 'faint';
    edges.push({ a, b, cls, hit: [a, b] });
  }
  // replaying (or a link with &step=N): show the task as it was after that many timeline steps
  const rp = S.replay && S.replay.key === S.sess.id + ':' + S.sess.tasks.indexOf(t) ? S.replay : (S.r.step ? { upto: +S.r.step } : null);
  const evs = rp ? t.events.slice(0, rp.upto) : t.events;
  const isQuestion = !touched.length && !(t.planned || []).length;
  const looked = isQuestion ? lookedFrom(evs, rp ? null : t.looked) : null;
  const answer = looked && looked.score.size ? buildStrands(looked) : null;
  if (answer) {
    // nodes not drawn last time fade in; S.shown remembers per task
    const key = S.sess.id + ':' + S.sess.tasks.indexOf(t) + (rp ? ':replay' : '');
    const shown = S.shownKey === key ? S.shown : null;
    if (S.strandOnly != null && S.strandOnly >= answer.list.length) S.strandOnly = null;
    const D = drawStrands(answer.list, looked, answer.isFocus, shown, answer.loose);
    mountSvg(D.svg, D.W, D.H);
    S.shownKey = key; S.shown = new Set(answer.list.flatMap((st, si) => [...st.nodes, ...st.sinks.map(k => k.id)].map(id => si + '|' + id)));
    wireNodes(n => go(n.startsWith('fn:') ? openRoute(n) : n.startsWith('f:') ? { v: 'file', id: n } : { v: 'map', id: n }));
    document.querySelectorAll('#svg .strand-h').forEach(h => h.onclick = () => {
      const i = +h.parentNode.dataset.strand; S.strandOnly = S.strandOnly === i ? null : i; render();
    });
    legend([['var(--accent)', '① ② = order Claude looked'], [null, 'Solid = read · dashed = a search matched it · green = where the flow starts. Click a row title to show only that flow.']]);
    tools(`<button class="btn" id="replay">${rp ? '■ Stop' : '▶ Replay'}</button><span>${rp ? `Step ${rp.upto} of ${t.events.length}` : 'Step through what Claude looked at, in order'}</span>${S.strandOnly != null ? '<button class="btn2" id="allflows">Show all flows</button>' : ''}`);
    $('replay').onclick = () => toggleReplay(t);
    if ($('allflows')) $('allflows').onclick = () => { S.strandOnly = null; render(); };
  } else if (rp && isQuestion) {
    showEmpty(`<h2>Replaying</h2><p>Step ${rp.upto} of ${t.events.length}: nothing looked at yet.</p>`);
    tools(`<button class="btn" id="replay">■ Stop</button><span>Step ${rp.upto} of ${t.events.length}</span>`);
    $('replay').onclick = () => toggleReplay(t);
  } else if (!ids.size) {
    showEmpty(`<h2>${t.prompt ? 'Claude is getting started' : 'Waiting'}</h2><p>Files will appear here as Claude reads and edits them.</p>`);
  } else {
    const L = layout([...ids], edges.map(e => [e.a, e.b]));
    const lookedIn = new Map((t.looked || []).map(x => [x.file, x.fns.map(f => fnShort(f.name))]));
    const nodes = [...ids].map(n => ({ id: n, cls: n.startsWith('new:') ? 'ghost' : (live.cls.get(n) || (reads.includes(n) ? 'read' : '')), badge: live.badge.get(n), badgeColor: live.badgeColor.get(n), stripe: n.startsWith('new:') ? 'var(--ghost)' : undefined, sub: !t.touched[n] && lookedIn.get(n)?.length ? 'looked at ' + lookedIn.get(n).join(', ') : undefined }));
    mountSvg(drawLanes(L, nodes, edges, { maxLabels: 0 }), L.W, L.H);
    wireNodes(n => { if (n.startsWith('new:')) return; go(kindOf(n) === 'file' ? { v: 'file', id: n } : { v: 'map', id: n }); });
  }
  const last = evs[evs.length - 1];
  const sm = t.summary;
  const [p, txt] = last ? evText(last) : [[], ''];
  const lastLine = !last ? '' : {
    prompt: `<b>You asked:</b> ${esc(trunc(t.prompt, 220))}`,
    read: `Claude is reading <b>${esc(txt)}</b>.`,
    search: `Claude is searching for ${esc(txt)}.`,
    run: `Claude ran <span class="code">${esc(txt)}</span>.`,
    plan: `<b>Claude made a plan.</b> ${esc(txt)}.`,
    create: `Claude created <b>${esc(txt)}</b>.`,
    edit: `${last.via === 'editor' ? 'You changed' : 'Claude changed'} <b>${esc(txt)}</b>.${last.off ? ' <span class="k-warn">This file was not in the plan.</span>' : ''}`,
    delete: `<b>${esc(txt)}</b> was deleted.`,
    test: `<b>Tests ran:</b> ${esc(txt)}.`,
    stop: `<b>Claude finished.</b>`,
  }[last.k] || esc(txt);
  const nLooked = (t.looked || []).reduce((n, x) => n + x.fns.length, 0);
  if (answer) $('readout').innerHTML = `${lastLine}<small>Question only, nothing changed · ${plural(nLooked, 'function')} looked at in ${plural((t.reads || []).length, 'file')} · the map shows where the clearest ones sit in the flow</small>`;
  else $('readout').innerHTML = `${lastLine}<small>${plural((t.reads || []).length, 'file')} read · ${sm.edited} edited, ${sm.created} created · ${(sm.fns.changed || 0) + (sm.fns.added || 0) + (sm.fns.removed || 0)} functions changed · <span class="k-up">${sm.ripple_files} other files call changed code</span>${sm.off_plan.length ? ` · <span class="k-warn">${sm.off_plan.length} outside the plan</span>` : ''}</small>`;

  const P = $('panel');
  const tasks = S.sess.tasks;
  const ti = tasks.indexOf(t);
  const t0 = t.started;
  const fmt = s => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const summaryKey = S.sess.id + ':' + ti;
  const sumState = S.summaries[summaryKey];
  P.innerHTML = `
    <div class="row-btns">
      <select id="sess" aria-label="Session">${S.sessions.map(s => `<option value="${esc(s.id)}" ${s.id === S.sess.id ? 'selected' : ''}>${s.source === 'editor' ? 'Your own edits' : 'Claude · ' + esc(trunc(s.prompt || s.id, 40))}</option>`).join('')}</select>
      ${tasks.length > 1 ? `<select id="task" aria-label="Request">${tasks.map((x, i) => `<option value="${i}" ${i === ti ? 'selected' : ''}>${i + 1}. ${esc(trunc(x.prompt || '(no prompt)', 46))}</option>`).join('')}</select>` : ''}
    </div>
    <div><h2 title="${esc(t.prompt || '')}">${esc(trunc(t.prompt || 'Session', 160))}</h2><p>${t.ended ? 'Finished' : 'Working'} · started ${new Date(t0 * 1000).toLocaleTimeString()}</p></div>
    <div class="stats" ${answer ? 'hidden' : ''}>
      <div class="stat acc"><b>${sm.edited}</b><span>files edited</span></div>
      <div class="stat down"><b>${sm.created}</b><span>files created</span></div>
      <div class="stat acc"><b>${(sm.fns.changed || 0) + (sm.fns.added || 0) + (sm.fns.removed || 0)}</b><span>functions changed</span></div>
      <div class="stat up"><b>${sm.ripple_files}</b><span>files to recheck</span></div>
      <div class="stat warn"><b>${sm.off_plan.length}</b><span>outside the plan</span></div>
      <div class="stat ${sm.tests?.failed ? 'warn' : 'down'}"><b>${sm.tests ? (sm.tests.failed ?? '?') : '–'}</b><span>tests failing</span></div>
    </div>
    ${sm.off_plan.map(f => `<div class="callout"><b>Look here first: ${esc(labelOf(f))}</b>Changed outside the plan: ${esc(Object.keys(t.touched[f].fns).join(', ') || 'no function changes')}</div>`).join('')}
    ${touched.length ? `<div><h3>Changed functions</h3><ul class="list">${touched.flatMap(f => Object.entries(t.touched[f].fns).map(([q, st]) => item([st === 'added' ? 'down' : st === 'removed' ? 'risk' : 'acc', st], esc(fnShort(q)), null, I.files.get(f)?.rel || f, st !== 'removed' ? goAttr({ v: 'file', id: f, fn: f.replace('f:', 'fn:') + '#' + q }) : ''))).join('')}</ul></div>` : ''}
    ${(t.looked || []).length ? `<div><h3>Functions Claude looked at</h3><ul class="list">${[...t.looked].reverse().flatMap(x => x.fns.map(f => [x, f])).sort((a, b) => (a[1].how === 'read' ? 0 : 1) - (b[1].how === 'read' ? 0 : 1) || (b[1].score || 0) - (a[1].score || 0)).map(([x, f]) => item(['', f.how === 'read' ? 'read' : 'found'], esc(fnShort(f.name)), null, I.files.get(x.file)?.rel || x.file, goAttr(openRoute(x.file.replace('f:', 'fn:') + '#' + f.name)))).slice(0, 15).join('')}</ul></div>` : ''}
    ${ripple.length ? `<div><h3>Calls changed code (recheck)</h3><ul class="list">${ripple.slice(0, 20).map(f => item(['up', 'calls'], esc(labelOf(f)), null, I.files.get(f)?.rel, goAttr({ v: 'file', id: f }))).join('')}</ul></div>` : ''}
    <div class="row-btns"><button class="btn2" id="sumbtn" ${sumState === 'loading' ? 'disabled' : ''}>${sumState === 'loading' ? 'Summarizing…' : 'Summarize with Claude'}</button><span style="font-size:12px;color:var(--muted)">Optional. Runs <span class="code">claude -p</span> on the diff only when clicked.</span></div>
    ${sumState && sumState !== 'loading' ? `<div class="summary-text">${esc(sumState)}</div>` : ''}
    <div><h3>Timeline</h3><ol class="tl" id="tl">${t.events.map((e, i) => { const [pp, tx] = evText(e); return `<li class="${e.file ? 'click' : ''} ${i === evs.length - 1 ? 'last' : ''}" ${e.text && e.k !== 'prompt' ? `title="${esc(e.text)}"` : ''} ${e.file ? `data-go="${esc(JSON.stringify({ v: 'file', id: e.file }))}"` : ''}><span class="tm">${fmt(e.t - t0)}</span><span class="pill ${pp[0]}">${esc(pp[1])}</span><span class="x">${esc(tx)}</span></li>`; }).join('')}</ol></div>`;
  wireList(P);
  const tl = $('tl'); if (tl) { const at = rp && tl.querySelector('li.last'); if (at) tl.scrollTop = at.offsetTop - tl.clientHeight / 2; else tl.scrollTop = tl.scrollHeight; }
  $('sess').onchange = e => { S.taskIdx = null; go({ v: 'live', sid: e.target.value }); };
  if ($('task')) $('task').onchange = e => { S.taskIdx = +e.target.value; render(); };
  $('sumbtn').onclick = async () => {
    S.summaries[summaryKey] = 'loading'; render();
    try {
      const res = await (await fetch('/api/summarize', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: S.sess.id, task: ti }) })).json();
      S.summaries[summaryKey] = res.text || ('Could not summarize: ' + (res.error || 'unknown error'));
    } catch (err) { S.summaries[summaryKey] = 'Could not reach the Wiretap server.'; }
    if (S.r.v === 'live') render();
  };
}

// ---------- Trace ----------
function viewTrace() {
  const I = S.I;
  tools(`<label for="tq">Feature words</label><input type="text" id="tq" placeholder="e.g. cod, pay_later, payLater" value="${esc(S.traceQ)}"><button class="btn" id="tgo">Trace</button><span>Finds every function that mentions them, then follows shared tables and calls.</span>`);
  const run = () => { const q = $('tq').value.trim(); if (q) go({ v: 'trace', q }); };
  if (S.r.q && S.r.q !== S.traceQ) {
    S.traceQ = S.r.q; S.trace = null;
    $('tq').value = S.traceQ; $('tgo').disabled = true;
    fetch('/api/search?q=' + encodeURIComponent(S.traceQ)).then(r => r.json()).then(d => { S.trace = d; if (S.r.v === 'trace') render(); });
  }
  $('tgo').onclick = run;
  $('tq').onkeydown = e => { if (e.key === 'Enter') run(); };
  crumbs([['Trace a feature']]);
  legend([['var(--accent)', 'Mentions the words'], ['var(--ghost)', 'Uses the same tables (found by following data)'], ['var(--down)', 'Table']]);
  const T = S.trace;
  if (!T && S.traceQ) { showEmpty('<h2>Tracing…</h2>'); $('readout').innerHTML = 'Searching every file.'; $('panel').innerHTML = ''; return; }
  if (!T) {
    showEmpty(`<h2>Trace a feature through every app</h2><p>Type the words the code uses for it. Different apps often name the same thing differently, so list them all, separated by commas.</p>`);
    $('readout').innerHTML = 'Type a few words and press Trace.';
    $('panel').innerHTML = '<p>Results show here.</p>';
    return;
  }
  const hitFns = T.hits.filter(h => h.id.startsWith('fn:'));
  const fileScore = new Map();
  for (const h of T.hits) { const f = h.id.startsWith('fn:') ? fileOfFn(h.id) : h.id; if (f) fileScore.set(f, (fileScore.get(f) || 0) + h.count); }
  const dataFiles = new Map();
  for (const fn of T.by_data) { const f = fileOfFn(fn); if (f && !fileScore.has(f)) dataFiles.set(f, (dataFiles.get(f) || 0) + 1); }
  const top = [...fileScore.entries()].sort((a, b) => b[1] - a[1]).slice(0, 26).map(x => x[0]);
  const topData = [...dataFiles.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(x => x[0]);
  const tables = T.tables.slice(0, 10);
  const ids = new Set([...top, ...topData, ...tables]);
  const edges = [];
  for (const a of ids) for (const [b] of (I.fout.get(a) || new Map())) if (ids.has(b)) edges.push({ a, b, cls: fileScore.has(a) && (fileScore.has(b) || b.startsWith('tb:')) ? 'hot' : 'faint', hit: [a, b] });
  const L = layout([...ids], edges.map(e => [e.a, e.b]), { rank: x => -(fileScore.get(x) || 0) });
  const nodes = [...ids].map(n => ({ id: n, cls: fileScore.has(n) ? 'lens' : n.startsWith('tb:') ? '' : 'data', badge: fileScore.has(n) ? String(fileScore.get(n)) : dataFiles.has(n) ? '!' : null, badgeColor: fileScore.has(n) ? 'var(--accent)' : 'var(--ghost)' }));
  if (ids.size) { mountSvg(drawLanes(L, nodes, edges, { maxLabels: 0 }), L.W, L.H); wireNodes(n => go(kindOf(n) === 'file' ? { v: 'file', id: n } : { v: 'map', id: n })); }
  else showEmpty('<h2>No matches</h2><p>Try other words, or the names used in the database.</p>');
  const apps = [...new Set([...fileScore.keys()].map(appOf).filter(Boolean))];
  $('readout').innerHTML = `<b>${plural(hitFns.length, 'function')}</b> in <b>${plural(fileScore.size, 'file')}</b> across ${esc(list(apps.map(appName)))} mention ${esc(T.terms.join(', '))}.
    <small><span class="k-gh">${T.by_data.length} more functions</span> touch the same ${plural(T.tables.length, 'table')} without using the words, and ${T.by_call.length} call into or out of them directly. Numbers on boxes count matching lines.</small>`;
  const byApp = new Map();
  for (const h of hitFns) { const a = appOf(h.id); if (!byApp.has(a)) byApp.set(a, []); byApp.get(a).push(h); }
  $('panel').innerHTML = `<div class="stats"><div class="stat acc"><b>${hitFns.length}</b><span>by name</span></div><div class="stat gh"><b>${T.by_data.length}</b><span>by shared tables</span></div><div class="stat"><b>${T.tables.length}</b><span>tables</span></div></div>
    ${[...byApp.entries()].map(([a, hs]) => `<div><h3>${esc(appName(a))}</h3><ul class="list">${hs.slice(0, 40).map(h => item(['acc', h.count], esc(labelOf(h.id)), esc(h.lines[0]?.[1] || ''), pathOf(h.id), goAttr(openRoute(h.id)))).join('')}</ul></div>`).join('')}
    ${T.by_data.length ? `<div><h3>Found by following data</h3><ul class="list">${T.by_data.slice(0, 40).map(id => item(['new', 'data'], esc(labelOf(id)), null, pathOf(id), goAttr(openRoute(id)))).join('')}</ul></div>` : ''}`;
  wireList($('panel'));
}

// ---------- Resources ----------
function viewResources() {
  const I = S.I;
  const res = S.g.nodes.filter(n => n.kind === 'resource');
  crumbs([['Shared resources']]);
  legend([['var(--warn)', 'Shared resource'], [null, 'Numbers count functions that use it']]);
  if (!res.length) { showEmpty('<h2>No shared resources found</h2>'); $('readout').innerHTML = ''; $('panel').innerHTML = ''; return; }
  const sel = res.find(n => n.id === S.r.id) || res.find(n => n.rkind === 'threadpool') || res[0];
  tools(`<label>Resource <select id="rsel">${res.map(n => `<option value="${esc(n.id)}" ${n.id === sel.id ? 'selected' : ''}>${esc(appName(n.app))} · ${esc(n.label)}</option>`).join('')}</select></label>`);
  $('rsel').onchange = e => go({ v: 'resources', id: e.target.value });
  const users = [...new Map((I.inn.get(sel.id) || []).filter(e => e.s.startsWith('fn:')).map(e => [e.s, e])).values()];
  const byFile = new Map();
  for (const e of users) { const f = fileOfFn(e.s); byFile.set(f, (byFile.get(f) || 0) + 1); }
  const files = [...byFile.entries()].sort((a, b) => b[1] - a[1]);
  const shown = files.slice(0, 14);
  const userFns = new Set(users.map(e => e.s));
  // what those functions also share
  const also = new Map();
  for (const fn of userFns) for (const e of (I.out.get(fn) || [])) if (e.t.startsWith('res:') && e.t !== sel.id) also.set(e.t, (also.get(e.t) || 0) + 1);
  const tablesTouched = new Set();
  for (const fn of userFns) for (const e of (I.out.get(fn) || [])) if (e.t.startsWith('tb:')) tablesTouched.add(e.t);
  const LX = 16, LW2 = 280, TX = 380, TW = 250, CX = 690, CW = 250, R = 46, TOPY = 50;
  const H = TOPY + Math.max(shown.length, 5) * R + 60, W = CX + CW + 16;
  const mid = TOPY + Math.max(shown.length, 5) * R / 2;
  let s = defs() + `<text class="lane-t" x="${LX}" y="30">Code that uses it</text><text class="lane-t" x="${TX}" y="30">Shared</text><text class="lane-t" x="${CX}" y="30">Also shared by the same code</text>`;
  const box = (x, y, w, h, t, sub, hot) => `<g class="ibox ${hot ? 'hot' : ''}" transform="translate(${x},${y})"><rect width="${w}" height="${h}" rx="8"/><text class="big" x="12" y="22">${esc(t)}</text><text class="s" x="12" y="40">${esc(sub)}</text></g>`;
  shown.forEach(([f, n], i) => {
    const y = TOPY + i * R;
    s += link(LX + LW2, y + 20, TX, mid + 4 + (i - shown.length / 2) * 4, '', null, false);
    s += row(LX, y, LW2, labelOf(f), `${plural(n, 'function')} · ${subOf(f)}`, 'file', `data-go="${esc(JSON.stringify({ v: 'map', id: f }))}"`);
  });
  s += box(TX, mid - 34, TW, 76, sel.label, `${users.length} functions in ${files.length} files`, true);
  let ay = mid - 60;
  for (const [rid, n] of [...also.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3)) {
    const rn = I.nodes.get(rid);
    s += link(TX + TW, mid, CX, ay + 28, 'warn', `${n} of ${userFns.size}`, true);
    s += box(CX, ay, CW, 56, rn.label, appName(rn.app), false);
    ay += 72;
  }
  if (tablesTouched.size) { s += link(TX + TW, mid + 10, CX, ay + 28, '', null, false); s += box(CX, ay, CW, 56, plural(tablesTouched.size, 'table'), 'touched by the same functions', false); }
  mountSvg(s, W, H);
  $('svg').querySelectorAll('.row').forEach(el => el.addEventListener('click', () => go(JSON.parse(el.dataset.go))));
  $('readout').innerHTML = `<b>${users.length} functions</b> in ${plural(files.length, 'file')} share <b>${esc(sel.label)}</b> in ${esc(appName(sel.app))}.
    <small>${esc(sel.desc || '')} If a few slow calls hold it, everything else that needs it waits.</small>`;
  $('panel').innerHTML = `<div><h2>${esc(sel.label)}</h2><p>${esc(sel.desc || '')}</p></div>
    <div><h3>What the map knows from code</h3><ul class="list">
      ${item(['acc', users.length], 'Functions that use it', `${files.length} files`)}
      ${[...also.entries()].map(([rid, n]) => item(['risk', n], `Also use ${esc(I.nodes.get(rid).label)}`, `${n} of the ${userFns.size} functions`)).join('')}
      ${item(['acc', tablesTouched.size], 'Tables those functions touch')}
    </ul></div>
    <div><h3>To find who held it during an incident</h3><ul class="list">
      ${item(['new', 'add'], 'Timing per request and function', 'A small middleware, or OpenTelemetry')}
      ${item(['new', 'add'], 'A thread dump during the stall', 'py-spy dump --pid <pid> shows what each thread waits on')}
      ${item(['new', 'add'], 'Database view', 'pg_stat_activity: running queries and locks')}
    </ul></div>
    <div><h3>All files</h3><ul class="list">${files.map(([f, n]) => item(['acc', n], esc(labelOf(f)), null, pathOf(f), goAttr({ v: 'map', id: f }))).join('')}</ul></div>`;
  wireList($('panel'));
}

// ======================= search =======================

let searchSel = 0, searchItems = [];
function doSearch(q) {
  const box = $('results');
  q = q.trim().toLowerCase();
  if (!q || !S.I) { box.hidden = true; return; }
  const out = [];
  const score = (name, full) => { const n = name.toLowerCase(); return n === q ? 0 : n.startsWith(q) ? 1 : n.includes(q) ? 2 : full.toLowerCase().includes(q) ? 3 : 9; };
  for (const f of S.g.files) { const sc = score(f.label, f.rel); if (sc < 9) out.push({ sc, k: 'file', id: f.id, n: f.label, p: appName(f.app) + ' · ' + f.rel }); }
  for (const fn of S.g.fns) { const short = fn.name.split('.').pop(); const sc = score(short, fn.name); if (sc < 9) out.push({ sc: sc + 0.5, k: 'function', id: fn.id, n: fn.name, p: S.I.files.get(fn.file).rel + ':' + fn.line }); }
  for (const n of S.g.nodes) {
    if (n.kind === 'trigger') continue;
    const sc = score(n.label, n.label + ' ' + (n.path || '')); if (sc < 9) out.push({ sc: sc + 0.2, k: n.kind, id: n.id, n: n.label, p: subOf(n.id) });
  }
  out.sort((a, b) => a.sc - b.sc || a.n.length - b.n.length);
  searchItems = out.slice(0, 30); searchSel = 0;
  box.innerHTML = searchItems.length ? searchItems.map((r, i) => `<button data-i="${i}" class="${i === 0 ? 'on' : ''}"><span class="k">${esc(r.k)}</span><span><span class="n">${esc(r.n)}</span><span class="p">${esc(r.p)}</span></span></button>`).join('') : '<div style="padding:8px;color:var(--muted)">No matches</div>';
  box.hidden = false;
  box.querySelectorAll('button').forEach(b => b.addEventListener('mousedown', ev => { ev.preventDefault(); pickSearch(+b.dataset.i); }));
}
function pickSearch(i) {
  const r = searchItems[i]; if (!r) return;
  $('results').hidden = true; $('q').value = ''; $('q').blur();
  if (r.k === 'function') go({ v: 'file', id: fileOfFn(r.id), fn: r.id });
  else if (r.k === 'route') { const n = S.I.nodes.get(r.id); go(n.fn ? { v: 'file', id: fileOfFn(n.fn), fn: n.fn } : { v: 'map', id: r.id }); }
  else go({ v: 'map', id: r.id });
}
$('q').addEventListener('input', e => doSearch(e.target.value));
$('q').addEventListener('keydown', e => {
  const btns = [...$('results').querySelectorAll('button')];
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); searchSel = Math.max(0, Math.min(btns.length - 1, searchSel + (e.key === 'ArrowDown' ? 1 : -1)));
    btns.forEach((b, i) => b.classList.toggle('on', i === searchSel)); btns[searchSel]?.scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'Enter') pickSearch(searchSel);
  else if (e.key === 'Escape') { $('results').hidden = true; $('q').blur(); }
});
$('q').addEventListener('blur', () => setTimeout(() => $('results').hidden = true, 150));
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'SELECT') { e.preventDefault(); $('q').focus(); }
});
document.querySelectorAll('#views button').forEach(b => b.addEventListener('click', () => {
  const v = b.dataset.v;
  if (v === 'map') go({ v: 'map', id: S.lastMapId || (S.r.id && !S.r.id.startsWith('tg:') ? S.r.id : undefined) });
  else go({ v });
}));

// ======================= data loading + live updates =======================

async function loadGraph() {
  const g = await (await fetch('/api/graph')).json();
  S.g = g; S.I = buildIndex(g);
  S.I.filesByApp = new Map(); g.files.forEach(f => S.I.filesByApp.set(f.app, (S.I.filesByApp.get(f.app) || 0) + 1));
  $('proj').textContent = g.name;
  document.title = 'Wiretap · ' + g.name;
}
async function loadSession(id) {
  try {
    const data = await (await fetch('/api/session' + (id ? '?id=' + encodeURIComponent(id) : ''))).json();
    S.sess = data.session; S.sessions = data.sessions || [];
  } catch { /* server restarting */ }
}
let graphTimer = null;
function connect() {
  const es = new EventSource('/api/stream');
  const st = $('status');
  es.onopen = () => { st.classList.add('on'); st.querySelector('span').textContent = 'live'; };
  es.onerror = () => { st.classList.remove('on'); st.querySelector('span').textContent = 'reconnecting'; };
  es.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'graph') {
      clearTimeout(graphTimer);
      graphTimer = setTimeout(async () => { await loadGraph(); render(); }, 250);
    } else if (msg.type === 'session') {
      // a link with sid (the terminal button) pins the Live view to that one session
      const pinned = S.r.v === 'live' && S.r.sid;
      const follow = pinned ? msg.session.id === S.r.sid : (!S.sess || S.sess.id === msg.session.id || S.r.v !== 'live' || msg.session.source === 'claude');
      if (follow) {
        S.sess = msg.session;
        if (!S.sessions.some(s => s.id === msg.session.id)) loadSession(msg.session.id);
        if (S.r.v !== 'live') S.liveDot = true;
        render();
      }
    }
  };
}

(async function start() {
  S.r = parseRoute();
  try { await loadGraph(); }
  catch (e) { showEmpty('<h2>Could not reach the Wiretap server</h2><p>Start it with <code>python3 -m wiretap serve</code>.</p>'); return; }
  await loadSession(S.r.v === 'live' ? S.r.sid : null);
  render();
  if (!location.search.includes('static')) connect();  // ?static: no live updates (for screenshots)
})();

// remember the last map focus for the Map tab
window.addEventListener('hashchange', () => { if (S.r.v === 'map' && S.r.id) S.lastMapId = S.r.id; if (S.r.v === 'file' && S.r.id) S.lastMapId = S.r.id; });
