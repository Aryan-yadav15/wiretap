// HTTP calls from TypeScript/JavaScript: fetch(url, {method}) and a client object's api.get(url) / axios.post(url, ...).
// Which objects count as clients comes from the app's "httpClients" setting.
//
// The shape of every TypeScript adapter:
//   export default { name, kind, label, statement(ctx, st), call(ctx, n, c), file(ctx) }   (all hooks optional)
//   statement  each top-level statement, after the core has registered its functions
//   call       each call expression. c = {from, d, parts, method, obj, line}: `from` is the function it is in
//              (null at the top level), `d` the dotted callee ("api.get"). Return true to claim the call, so it
//              is not also recorded as a plain function call.
//   file       once the whole file has been read (file-based routing, for example)
//   ctx        {ts, sf, rel, out, state, lineOf, dotted, urlParts, fnLike, register, ownerOf, httpClients, add}
//              ctx.add('routes' | 'tables' | 'http' | ..., record, name) adds a record tagged with the adapter.
//              ctx.state is a scratch object for this adapter and this file.
// Record formats: docs/adapters.md.

const METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'request']);

export default {
  name: 'http', kind: 'http', label: 'HTTP calls (fetch and client objects)',
  call(ctx, n, c) {
    if (!c.from) return false;
    const { ts, sf } = ctx;
    if (c.parts.length >= 2 && ctx.httpClients.has(c.obj) && METHODS.has(c.method)) {
      ctx.add('http', { from: c.from, method: c.method.toUpperCase(), url: ctx.urlParts(n.arguments[0]), line: c.line }, 'http');
      return true;
    }
    if (c.d === 'fetch') {
      let m = 'GET';
      const opts = n.arguments[1];
      if (opts && ts.isObjectLiteralExpression(opts)) {
        for (const p of opts.properties) if (ts.isPropertyAssignment(p) && p.name.getText(sf) === 'method' && ts.isStringLiteral(p.initializer)) m = p.initializer.text.toUpperCase();
      }
      ctx.add('http', { from: c.from, method: m, url: ctx.urlParts(n.arguments[0]), line: c.line, fetch: true }, 'http');
      return true;
    }
    return false;
  },
};
