import axios from 'axios';

export async function loadOrder(id: string) {
  const res = await axios.get(`/api/orders/${id}`);
  return res.data;
}

export async function placeOrder(body: unknown) {
  return axios.post('/api/orders', body);
}
