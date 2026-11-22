// Prisma: prisma.orderItem.findMany(...), db.user.update(...), tx.order.create(...) inside $transaction.
// The model accessor (orderItem) is turned into its table by the linker, from schema.prisma (`@@map`) when there is one.

const READ = new Set(['findMany', 'findUnique', 'findFirst', 'findUniqueOrThrow', 'findFirstOrThrow', 'count', 'aggregate', 'groupBy']);
const WRITE = new Set(['create', 'createMany', 'createManyAndReturn', 'update', 'updateMany', 'upsert', 'delete', 'deleteMany']);
const CLIENT = /^(prisma|db|tx|client|prismaClient|this\.prisma|this\.db|ctx\.prisma|ctx\.db)$/;

export default {
  name: 'prisma', kind: 'tables', label: 'Prisma queries',
  call(ctx, n, c) {
    if (!c.from || c.parts.length < 3 || !(READ.has(c.method) || WRITE.has(c.method))) return false;
    const model = c.parts[c.parts.length - 2];
    const client = c.parts.slice(0, -2).join('.');
    if (!CLIENT.test(client) || model.startsWith('$')) return false;
    ctx.add('tables', { fn: c.from, table: 'prisma:' + model, op: READ.has(c.method) ? 'read' : 'write', line: c.line }, 'prisma');
    return true;
  },
};
