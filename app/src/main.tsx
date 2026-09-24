import { QueryClientProvider } from '@tanstack/react-query';
// import { ReactQueryDevtools } from '@tanstack/react-query-devtools';
import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './i18n';
import './index.css';
import { queryClient } from './lib/queryClient';
import { PlatformProvider } from './platform/PlatformContext';
import { createTauriPlatform } from './platform/tauriPlatform';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <PlatformProvider platform={createTauriPlatform()}>
      <QueryClientProvider client={queryClient}>
        <App />
      {/* <ReactQueryDevtools initialIsOpen={false} /> */}
      </QueryClientProvider>
    </PlatformProvider>
  </React.StrictMode>,
);
