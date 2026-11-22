// Supabase straight from TypeScript: supabase.from('orders').select(...), supabase.rpc('fn'), supabase.storage.from('bucket').

export default {
  name: 'supabase', kind: 'tables', label: 'Supabase tables (from the client)',
  call(ctx, n, c) {
    const { ts } = ctx;
    if (!c.from || !(c.method === 'from' || c.method === 'rpc') || !/supabase/i.test(c.obj) || !n.arguments[0] || !ts.isStringLiteral(n.arguments[0])) return false;
    let op = 'access';
    for (let p = n.parent; p && ts.isPropertyAccessExpression(p); p = p.parent?.parent) {
      if (p.name.text === 'select') op = 'read';
      if (['insert', 'upsert', 'update', 'delete'].includes(p.name.text)) op = 'write';
      if (!p.parent || !ts.isCallExpression(p.parent)) break;
    }
    const prefix = c.method === 'rpc' ? 'rpc:' : /storage$/i.test(c.obj) ? 'storage:' : '';
    ctx.add('tables', { fn: c.from, table: prefix + n.arguments[0].text, op: c.method === 'rpc' ? 'call' : op, line: c.line, client: true }, 'supabase');
    return true;
  },
};
