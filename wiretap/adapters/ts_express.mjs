// Express-style routes, which Fastify and Hono share: app.get('/orders/:id', handler), router.post(...),
// app.use('/api', ordersRouter) / app.route('/api', sub) to mount a router under a prefix.
// A call counts as a route when the object was made by express() / Router() / fastify() / new Hono() in this
// file, or when its last argument is a function (`app.get('/x', (req, res) => ...)` inside registerRoutes(app)).
// That keeps api.get('/orders', {params}) on the client an HTTP call, not a route.

const VERBS = { get: 'GET', post: 'POST', put: 'PUT', patch: 'PATCH', delete: 'DELETE', head: 'HEAD', options: 'OPTIONS', all: 'ANY' };
const MAKERS = /^(express|express\.Router|Router|fastify|Fastify|new Hono|new Elysia|new Router|new KoaRouter)$/;
const ROUTERS = /Router$/;

// /orders/:id -> /orders/{id}
const toPath = p => p.replace(/:(\w+)\??/g, '{$1}');

export default {
  name: 'express', kind: 'routes', label: 'Express / Fastify / Hono routes',
  statement(ctx, st) {
    const { ts } = ctx;
    if (!ts.isVariableStatement(st)) return;
    for (const d of st.declarationList.declarations) {
      if (!ts.isIdentifier(d.name) || !d.initializer) continue;
      const init = d.initializer;
      const callee = ts.isCallExpression(init) ? ctx.dotted(init.expression) : ts.isNewExpression(init) ? 'new ' + ctx.dotted(init.expression) : null;
      if (!callee || !MAKERS.test(callee)) continue;
      if (ROUTERS.test(callee)) ctx.out.routers[d.name.text] = '';
      else ctx.out.app_vars.push(d.name.text);
    }
  },
  call(ctx, n, c) {
    const { ts } = ctx;
    const args = n.arguments;
    if (c.parts.length < 2 || !args.length) return false;
    const known = c.obj in ctx.out.routers || ctx.out.app_vars.includes(c.obj);
    // mounting: app.use('/api', router) / app.use(router) / app.route('/api', sub)
    if ((c.method === 'use' || c.method === 'route') && known) {
      const hasPrefix = ts.isStringLiteral(args[0]) || ts.isNoSubstitutionTemplateLiteral(args[0]);
      const child = args[hasPrefix ? 1 : 0];
      const prefix = hasPrefix ? toPath(args[0].text) : '';
      if (child && ctx.dotted(child)) ctx.add('includes', { parent: c.obj, child: ctx.dotted(child), prefix }, 'express');
      else if (child && ts.isCallExpression(child) && ctx.dotted(child.expression) === 'require' && child.arguments[0] && ts.isStringLiteral(child.arguments[0])) {
        ctx.add('includes', { parent: c.obj, spec: child.arguments[0].text, prefix }, 'express');
      } else return false;
      return true;
    }
    const verb = VERBS[c.method];
    if (!verb || !(ts.isStringLiteral(args[0]) || ts.isNoSubstitutionTemplateLiteral(args[0])) || !/^[/*]/.test(args[0].text)) return false;
    const last = args[args.length - 1];
    const fn = ctx.fnLike(last);
    if (!known && !(fn && args.length >= 2)) return false;
    const path = toPath(args[0].text);
    const rec = { owner: c.obj, method: verb, path, line: c.line };
    if (fn) {
      // an inline handler becomes a function of its own, named after its route
      let qual = `${verb} ${path}`;
      if (ctx.out.functions.some(f => f.name === qual) || ctx.taken(qual)) qual += ` (line ${c.line})`;
      ctx.register(fn, qual, c.from);
      rec.fn = qual;
    } else if (ctx.dotted(last)) {
      rec.target = ctx.dotted(last);   // a handler defined elsewhere: resolved by the linker
      rec.from = c.from;
    } else return false;
    ctx.add('routes', rec, 'express');
    return true;
  },
};
