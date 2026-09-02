/**
 * Dashboard entry point (referenced by index.html as /src/main.tsx).
 *
 * BrowserRouter (not HashRouter) because both servers in front of this app do an
 * index.html fallback: nginx `try_files $uri $uri/ /index.html` in production,
 * and Vite's dev server by default.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import App from './App';
import './index.css';

const container = document.getElementById('root');

if (!container) {
  // Fail loudly: a silent blank page here is the single most confusing failure
  // mode of an SPA, and it only happens if index.html was edited or replaced.
  throw new Error(
    'Hermes dashboard cannot mount: no element with id="root" was found. ' +
      'Check services/dashboard/index.html.',
  );
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
