const prisma = require('../db');

async function getOrder(req, res) {
  const order = await prisma.order.findUnique({ where: { id: req.params.id } });
  res.json(order);
}

module.exports = { getOrder };
