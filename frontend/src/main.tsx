import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import 'emoogle-emoji-picker/dist/index.css';
import './styles/globals.css';
import App from './App.tsx';
import { AgentArchitectureAdmin } from './components/admin/AgentArchitectureAdmin.tsx';
import { AccountBoundary } from './components/account/AccountBoundary.tsx';
import './styles/dark-theme.css';

const isArchitectureAdmin = window.location.pathname.replace(/\/$/, '') === '/agent-architecture';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AccountBoundary>
      {isArchitectureAdmin ? <AgentArchitectureAdmin standalone /> : <App />}
    </AccountBoundary>
  </StrictMode>,
);
