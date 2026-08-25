# Drasil web client

The React, TypeScript, and Vite client for Drasil. Project-wide setup,
architecture, testing, and contribution instructions live in the repository
root [README](../README.md).

In local development, Vite proxies `/api` to `DRASIL_BACKEND_ORIGIN` (default
`http://127.0.0.1:8000`). Production builds also support runtime server
selection: leave the backend field blank for a same-origin install, or enter an
HTTPS backend address on the sign-in screen. The address is saved in local
storage and the account session token is kept in session storage for the
current tab.

`VITE_DRASIL_BACKEND_URL` may provide a non-secret default at build time, but
it is optional and must never contain credentials.

Common commands:

```sh
npm ci
npm run dev
npm run lint
npm run build
npm run test:e2e:local
```
