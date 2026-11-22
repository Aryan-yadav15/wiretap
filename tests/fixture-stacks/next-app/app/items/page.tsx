'use client';

export default function ItemsPage({ id }: { id: string }) {
  async function remove() {
    await fetch(`/api/items/${id}`, { method: 'DELETE' });
  }
  async function check() {
    await fetch('/api/health');
  }
  return <button onClick={remove}>Remove</button>;
}
