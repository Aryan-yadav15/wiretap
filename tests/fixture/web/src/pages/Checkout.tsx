import React, { useState } from 'react';
import { createOrder } from '../services/api';

export default function Checkout() {
  const [method, setMethod] = useState('online');

  const handlePlace = async () => {
    await createOrder([], method);
  };

  return (
    <div>
      <button onClick={() => setMethod('cod')}>Pay later</button>
      <button onClick={handlePlace}>Place Order</button>
    </div>
  );
}
