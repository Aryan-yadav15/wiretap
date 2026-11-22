const express = require('express');
const { getOrder } = require('../controllers/orders');
const prisma = require('../db');

const router = express.Router();

router.get('/:id', getOrder);
router.post('/', async (req, res) => {
  const order = await prisma.order.create({ data: req.body });
  await prisma.orderItem.createMany({ data: req.body.items });
  res.json(order);
});

module.exports = router;
