// Extract functions, calls, HTTP calls, JSX renders and UI triggers from TS/TSX/JS files.
// Input (stdin): {"typescript": "/abs/path/to/node_modules/typescript", "httpClients": ["api"], "files": [{"abs": "...", "rel": "..."}]}
// Output (stdout): JSON array, one entry per file. Name resolution across files happens in graph.py.

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const input = JSON.parse(readFileSync(0, 'utf8'));
const require = createRequire(import.meta.url);
const ts = require(input.typescript);
const HTTP_CLIENTS = new Set(input.httpClients || ['api']);
const HTTP_METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'request']);
const WRAPPERS = new Set(['useCallback', 'useMemo', 'memo', 'forwardRef', 'React.memo', 'React.forwardRef', 'React.useCallback', 'observer']);

const hash = s => createHash('sha1').update(s).digest('hex').slice(0, 12);

function kindFor(rel) {
  if (rel.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (rel.endsWith('.jsx')) return ts.ScriptKind.JSX;
  if (rel.endsWith('.js') || rel.endsWith('.mjs')) return ts.ScriptKind.JSX; // JS files often contain JSX
  return ts.ScriptKind.TS;
}

// Unwrap useCallback(() => ...), memo(function X() {...}) etc. Returns the function node or null.
function fnLike(node) {
  if (!node) return null;
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return node;
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression?.(node)) return fnLike(node.expression);
  if (ts.isCallExpression(node)) {
    const callee = node.expression.getText();
    if (WRAPPERS.has(callee) && node.arguments.length) return fnLike(node.arguments[0]);
  }
  return null;
}

function dotted(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (node.kind === ts.SyntaxKind.ThisKeyword) return 'this';
  if (ts.isPropertyAccessExpression(node)) {
    const left = dotted(node.expression);
    return left ? left + '.' + node.name.text : null;
  }
  return null;
}

// URL expression -> list of parts: {s: "literal"} | {ref: "A.B", call?: true} | {p: 1}
function urlParts(node) {
  if (!node) return null;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return [{ s: node.text }];
  if (ts.isTemplateExpression(node)) {
    const parts = [{ s: node.head.text }];
    for (const span of node.templateSpans) {
      const d = dotted(span.expression);
      if (d && /^[A-Z_][A-Z0-9_]*(\.|$)/.test(d)) parts.push({ ref: d });
      else if (ts.isCallExpression(span.expression) && dotted(span.expression.expression)) parts.push({ ref: dotted(span.expression.expression), call: true });
      else parts.push({ p: 1 });
      parts.push({ s: span.literal.text });
    }
    return parts;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const a = urlParts(node.left), b = urlParts(node.right);
    return [...(a || [{ p: 1 }]), ...(b || [{ p: 1 }])];
  }
  if (ts.isCallExpression(node) && dotted(node.expression)) return [{ ref: dotted(node.expression), call: true }];
  const d = dotted(node);
  if (d) return [{ ref: d }];
  return null;
}

// Visible text of a JSX element, including both branches of {busy ? "Processing..." : "Place Order"}.
function jsxText(node, sf) {
  const out = [];
  const walk = n => {
    if (ts.isJsxAttributes(n)) return;
    if (ts.isJsxText(n)) { const t = n.getText(sf).trim(); if (t) out.push(t); }
    else if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) { if (n.text.trim()) out.push(n.text.trim()); }
    ts.forEachChild(n, walk);
  };
  if (ts.isJsxElement(node)) node.children.forEach(walk);
  const s = [...new Set(out)].join(' / ').replace(/\s+/g, ' ');
  return s.length > 44 ? s.slice(0, 42) + '…' : s;
}

function extract(file) {
  const text = readFileSync(file.abs, 'utf8');
  const sf = ts.createSourceFile(file.rel, text, ts.ScriptTarget.Latest, true, kindFor(file.rel));
  const lineOf = pos => sf.getLineAndCharacterOfPosition(pos).line + 1;
  const out = {
    lang: 'ts', rel: file.rel, imports: [], reexports: [], exports: {}, defaultExport: null,
    functions: [], calls: [], http: [], renders: [], triggers: [], consts: {}, destructured: [], tables: [], instances: {},
    lines: text.split('\n').length, error: null,
  };
  const fnNodes = new Map(); // node -> qual
  const fns = [];

  // ---- imports / exports ----
  for (const st of sf.statements) {
    if (ts.isImportDeclaration(st) && ts.isStringLiteral(st.moduleSpecifier)) {
      const imp = { spec: st.moduleSpecifier.text, names: [], def: null, ns: null };
      const c = st.importClause;
      if (c) {
        if (c.name) imp.def = c.name.text;
        if (c.namedBindings) {
          if (ts.isNamespaceImport(c.namedBindings)) imp.ns = c.namedBindings.name.text;
          else for (const el of c.namedBindings.elements) imp.names.push({ local: el.name.text, imported: (el.propertyName || el.name).text });
        }
      }
      out.imports.push(imp);
    } else if (ts.isExportDeclaration(st) && st.moduleSpecifier && ts.isStringLiteral(st.moduleSpecifier)) {
      const names = st.exportClause && ts.isNamedExports(st.exportClause)
        ? st.exportClause.elements.map(el => ({ local: el.name.text, imported: (el.propertyName || el.name).text }))
        : null; // export * from
      out.reexports.push({ spec: st.moduleSpecifier.text, names });
    } else if (ts.isExportDeclaration(st) && st.exportClause && ts.isNamedExports(st.exportClause)) {
      for (const el of st.exportClause.elements) out.exports[el.name.text] = (el.propertyName || el.name).text;
    } else if (ts.isExportAssignment(st)) {
      const f = fnLike(st.expression);
      if (ts.isIdentifier(st.expression)) out.defaultExport = st.expression.text;
      else if (f) { out.defaultExport = 'default'; register(f, 'default', null, st); }
      else if (ts.isCallExpression(st.expression)) {
        // export default connect(...)(Comp) / memo(Comp)
        const last = st.expression.arguments[st.expression.arguments.length - 1];
        if (last && ts.isIdentifier(last)) out.defaultExport = last.text;
      }
    }
  }

  function isExported(st) {
    return !!(st.modifiers && st.modifiers.some(m => m.kind === ts.SyntaxKind.ExportKeyword));
  }
  function isDefault(st) {
    return !!(st.modifiers && st.modifiers.some(m => m.kind === ts.SyntaxKind.DefaultKeyword));
  }

  function register(node, qual, parent, holder) {
    const startNode = holder || node;
    const rec = { name: qual, line: lineOf(startNode.getStart(sf)), end: lineOf(node.end), parent, node, holder: startNode };
    fnNodes.set(node, qual);
    fns.push(rec);
    return rec;
  }

  // Constants like API_ENDPOINTS = { LOGIN: '/api/v1/...', ORDER_ACCEPT: (id) => `/api/v1/orders/${id}/accept` }
  function recordConst(name, init) {
    if (!init) return;
    const parts = urlParts(init);
    if (parts && parts.every(p => p.s !== undefined || p.p)) { out.consts[name] = parts; return; }
    let obj = init;
    while (obj && (ts.isAsExpression(obj) || ts.isParenthesizedExpression(obj) || ts.isSatisfiesExpression?.(obj))) obj = obj.expression;
    if (obj && ts.isObjectLiteralExpression(obj)) {
      for (const p of obj.properties) {
        if (!ts.isPropertyAssignment(p) || !p.name) continue;
        const key = p.name.getText(sf).replace(/['"]/g, '');
        let v = p.initializer;
        const f = fnLike(v);
        if (f && f.body && !ts.isBlock(f.body)) v = f.body;
        const pp = urlParts(v);
        if (pp) out.consts[name + '.' + key] = pp;
      }
    }
  }

  // ---- top-level declarations ----
  for (const st of sf.statements) {
    if (ts.isFunctionDeclaration(st) && st.name) {
      register(st, st.name.text, null);
      if (isExported(st)) out.exports[st.name.text] = st.name.text;
      if (isDefault(st)) out.defaultExport = st.name.text;
    } else if (ts.isFunctionDeclaration(st) && isDefault(st)) {
      register(st, 'default', null); out.defaultExport = 'default';
    } else if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (!ts.isIdentifier(d.name)) continue;
        const f = fnLike(d.initializer);
        if (f) register(f, d.name.text, null, d);
        else {
          recordConst(d.name.text, d.initializer);
          let init = d.initializer;
          while (init && (ts.isAsExpression(init) || ts.isParenthesizedExpression(init) || ts.isSatisfiesExpression?.(init))) init = init.expression;
          // export const admin = { getZones: async () => ... }  ->  functions "admin.getZones"
          if (init && ts.isObjectLiteralExpression(init)) {
            for (const p of init.properties) {
              if (ts.isPropertyAssignment(p) && p.name && fnLike(p.initializer)) register(fnLike(p.initializer), d.name.text + '.' + p.name.getText(sf).replace(/['"]/g, ''), null, p);
              else if (ts.isMethodDeclaration(p) && p.body) register(p, d.name.text + '.' + p.name.getText(sf), null);
            }
          }
          // export const orderService = new OrderService()
          if (init && ts.isNewExpression(init) && ts.isIdentifier(init.expression)) out.instances[d.name.text] = init.expression.text;
        }
        if (isExported(st)) out.exports[d.name.text] = d.name.text;
      }
    } else if (ts.isClassDeclaration(st) && st.name) {
      const cls = st.name.text;
      if (isExported(st)) out.exports[cls] = cls;
      if (isDefault(st)) out.defaultExport = cls;
      for (const m of st.members) {
        if ((ts.isMethodDeclaration(m) || ts.isConstructorDeclaration(m)) && m.body) {
          const mname = ts.isConstructorDeclaration(m) ? 'constructor' : m.name.getText(sf);
          register(m, cls + '.' + mname, null);
        } else if (ts.isPropertyDeclaration(m) && fnLike(m.initializer)) {
          register(fnLike(m.initializer), cls + '.' + m.name.getText(sf), null, m);
        }
      }
    }
  }

  // ---- nested named functions (handlers inside components), up to 2 levels ----
  function nested(rec, depth) {
    if (depth > 2) return;
    const body = rec.node.body;
    if (!body) return;
    const visit = n => {
      if (n !== body && (ts.isArrowFunction(n) || ts.isFunctionExpression(n) || ts.isFunctionDeclaration(n)) && fnNodes.has(n)) return;
      if (ts.isFunctionDeclaration(n) && n.name) {
        const r = register(n, rec.name + '.' + n.name.text, rec.name);
        nested(r, depth + 1);
        return;
      }
      if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name)) {
        const f = fnLike(n.initializer);
        if (f && !fnNodes.has(f)) {
          const r = register(f, rec.name + '.' + n.name.text, rec.name, n);
          nested(r, depth + 1);
          return;
        }
      }
      ts.forEachChild(n, visit);
    };
    ts.forEachChild(body, visit);
  }
  for (const rec of [...fns]) nested(rec, 1);

  // ---- per-function bodies: calls, http, jsx ----
  const ownerOf = node => {
    for (let n = node.parent; n; n = n.parent) if (fnNodes.has(n)) return fnNodes.get(n);
    return null;
  };

  const visitAll = n => {
    if (ts.isCallExpression(n)) {
      const from = ownerOf(n);
      const callee = n.expression;
      const d = dotted(callee);
      const line = lineOf(n.getStart(sf));
      if (from && d) {
        const parts = d.split('.');
        const method = parts[parts.length - 1];
        const obj = parts.slice(0, -1).join('.');
        if (parts.length >= 2 && HTTP_CLIENTS.has(obj) && HTTP_METHODS.has(method)) {
          const url = urlParts(n.arguments[0]);
          out.http.push({ from, method: method.toUpperCase(), url, line });
        } else if (d === 'fetch') {
          let m = 'GET';
          const opts = n.arguments[1];
          if (opts && ts.isObjectLiteralExpression(opts)) {
            for (const p of opts.properties) if (ts.isPropertyAssignment(p) && p.name.getText(sf) === 'method' && ts.isStringLiteral(p.initializer)) m = p.initializer.text.toUpperCase();
          }
          out.http.push({ from, method: m, url: urlParts(n.arguments[0]), line, fetch: true });
        } else if ((method === 'from' || method === 'rpc') && /supabase/i.test(obj) && n.arguments[0] && ts.isStringLiteral(n.arguments[0])) {
          // Direct database access from the client: supabase.from('orders').select(...)
          let op = 'access';
          for (let p = n.parent; p && ts.isPropertyAccessExpression(p); p = p.parent?.parent) {
            if (['select'].includes(p.name.text)) op = 'read';
            if (['insert', 'upsert', 'update', 'delete'].includes(p.name.text)) op = 'write';
            if (!p.parent || !ts.isCallExpression(p.parent)) break;
          }
          const prefix = method === 'rpc' ? 'rpc:' : /storage$/i.test(obj) ? 'storage:' : '';
          out.tables.push({ fn: from, table: prefix + n.arguments[0].text, op: method === 'rpc' ? 'call' : op, line });
        } else {
          out.calls.push({ from, target: d, line });
        }
      }
      // Functions passed as callbacks: setTimeout(handleX), onSomething(handleX), arr.map(renderRow)
      if (from) for (const a of n.arguments) {
        if (ts.isIdentifier(a)) out.calls.push({ from, target: a.text, line, ref: true });
      }
    }
    if (ts.isVariableDeclaration(n) && ts.isObjectBindingPattern(n.name) && n.initializer && ts.isCallExpression(n.initializer)) {
      const hook = dotted(n.initializer.expression);
      const from = ownerOf(n);
      if (hook && from) for (const el of n.name.elements) {
        const local = el.name.getText(sf), prop = el.propertyName ? el.propertyName.getText(sf) : local;
        out.destructured.push({ from, local, prop, hook });
      }
    }
    if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
      const from = ownerOf(n);
      const tag = n.tagName.getText(sf);
      const line = lineOf(n.getStart(sf));
      if (from && /^[A-Z]/.test(tag)) out.renders.push({ from, tag, line });
      if (from) {
        const label = ts.isJsxOpeningElement(n) ? jsxText(n.parent, sf) : '';
        for (const attr of n.attributes.properties) {
          if (!ts.isJsxAttribute(attr) || !attr.initializer || !ts.isJsxExpression(attr.initializer) || !attr.initializer.expression) continue;
          const ev = attr.name.getText(sf);
          if (!/^on[A-Z]/.test(ev)) continue;
          const e = attr.initializer.expression;
          const targets = [];
          const d = dotted(e);
          if (d) targets.push(d);
          else if (ts.isArrowFunction(e) || ts.isFunctionExpression(e)) {
            const walk = x => { if (ts.isCallExpression(x)) { const cd = dotted(x.expression); if (cd) targets.push(cd); } ts.forEachChild(x, walk); };
            walk(e.body);
          }
          for (const t of targets) out.triggers.push({ from, target: t, event: ev, tag, text: label, line });
        }
      }
    }
    ts.forEachChild(n, visitAll);
  };
  visitAll(sf);

  // Hash each function's own text, excluding nested functions, so an edit to a handler doesn't mark its parent changed.
  for (const rec of fns) {
    const node = rec.holder;
    let s = text.slice(node.getStart(sf), node.end);
    const base = node.getStart(sf);
    const kids = fns.filter(r => r.parent === rec.name).map(r => [r.holder.getStart(sf) - base, r.holder.end - base]).sort((a, b) => b[0] - a[0]);
    for (const [a, b] of kids) if (a >= 0 && b <= s.length) s = s.slice(0, a) + s.slice(b);
    out.functions.push({ name: rec.name, line: rec.line, end: rec.end, parent: rec.parent, hash: hash(s), component: /^[A-Z]/.test(rec.name.split('.').pop()) });
  }
  return out;
}

const results = [];
for (const f of input.files) {
  try { results.push(extract(f)); }
  catch (e) { results.push({ lang: 'ts', rel: f.rel, error: String(e && e.message || e), functions: [], calls: [], http: [], renders: [], triggers: [], imports: [], reexports: [], exports: {}, consts: {}, destructured: [], lines: 0 }); }
}
process.stdout.write(JSON.stringify(results));
