// File-based API routes:
//   Next.js app router   app/api/orders/[id]/route.ts  exporting GET / POST / ...   -> GET /api/orders/{id}
//   Next.js pages router pages/api/orders/[id].ts      with a default export        -> ANY /api/orders/{id}
//   Expo Router          app/api/orders+api.ts         exporting GET / POST / ...   -> GET /api/orders
// (group) folders and @slots don't appear in the URL; [id] and [...slug] become {id} / {slug}.

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'];

function urlOf(dirs) {
  const segs = dirs.filter(s => s && !/^\(.*\)$/.test(s) && !s.startsWith('@'))
    .map(s => s.replace(/^\[\[?\.{3}(.+?)\]\]?$/, '{$1}').replace(/^\[(.+)\]$/, '{$1}'));
  return '/' + segs.join('/');
}

export default {
  name: 'nextjs', kind: 'routes', label: 'Next.js / Expo Router API routes',
  file(ctx) {
    const rel = ctx.rel.replace(/\\/g, '/');
    const fns = new Map(ctx.out.functions.map(f => [f.name, f]).concat(ctx.pending().map(f => [f.name, f])));
    const add = (method, name, path) => {
      const f = fns.get(name);
      ctx.add('routes', { fn: f ? name : null, method, path, line: f ? f.line : 1 }, 'nextjs');
    };
    let m = rel.match(/(?:^|\/)app\/(.*?)\/?route\.(?:ts|js|tsx|jsx|mjs)$/);
    if (m) {
      const path = urlOf(m[1].split('/'));
      for (const meth of METHODS) if (meth in ctx.out.exports) add(meth, ctx.out.exports[meth], path);
      return;
    }
    m = rel.match(/(?:^|\/)app\/(.*)\+api\.(?:ts|js|tsx|jsx)$/);
    if (m) {
      const dirs = m[1].split('/');
      if (dirs[dirs.length - 1] === 'index') dirs.pop();
      for (const meth of METHODS) if (meth in ctx.out.exports) add(meth, ctx.out.exports[meth], urlOf(dirs));
      return;
    }
    m = rel.match(/(?:^|\/)pages\/(api\/.*)\.(?:ts|js|tsx|jsx)$/);
    if (m && ctx.out.defaultExport) {
      const dirs = m[1].split('/');
      if (dirs[dirs.length - 1] === 'index') dirs.pop();
      add('ANY', ctx.out.defaultExport, urlOf(dirs));
    }
  },
};
