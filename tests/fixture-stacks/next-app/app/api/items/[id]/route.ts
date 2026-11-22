import { prisma } from '../../../../lib/db';

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const item = await prisma.item.findUnique({ where: { id: params.id } });
  return Response.json(item);
}

export async function DELETE(req: Request, { params }: { params: { id: string } }) {
  await prisma.item.delete({ where: { id: params.id } });
  return new Response(null, { status: 204 });
}
