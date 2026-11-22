import axios from 'axios';

// the base URL (https://api.example.com/v1) carries the /v1 prefix, so the paths here leave it out
export const getOrder = (id: number) => axios.get(`/orders/${id}`);
export const deleteOrder = (id: number) => axios.delete(`/orders/${id}`);
export const recentOrders = () => axios.get('/orders/recent');
