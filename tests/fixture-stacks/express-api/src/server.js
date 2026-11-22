const express = require('express');
const orders = require('./routes/orders');

const app = express();
app.use('/api/orders', orders);
app.get('/health', (req, res) => res.json({ ok: true }));

module.exports = app;
