import axios from 'axios';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const api = axios.create({ baseURL: API_URL });

export const createOrder = async (items: unknown[], method: string) => {
  const res = await api.post('/api/orders/', { items, method });
  return res.data;
};

export const getOrder = async (id: string) => (await api.get(`/api/orders/${id}`)).data;

export const getMissing = async () => (await api.get('/api/nothing-here')).data;

export default api;
