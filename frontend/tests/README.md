# Frontend E2E Test Framework

This frontend workspace uses Playwright for end-to-end coverage and deterministic UI smoke tests.

## Setup

1. Copy `.env.example` to `.env` if you need custom test settings.
2. Run `npm install`.
3. Run `npm run test:e2e:install` once to download the Chromium browser used by the sample suite.

## Running Tests

- `npm run test:e2e:local` starts Vite and a managed FastAPI server backed by a fresh
  temporary SQLite task/checkpoint database, waits for both, runs Playwright through
  the normal HTTP proxy, and shuts both down. The isolated server uses Drasil's test
  principal, so the suite cannot read or modify the personal development database.
- `npm run test:e2e:local:headed` uses the same managed local flow with a visible browser.
- `npm run test:e2e:local:debug` uses the same managed local flow with Playwright Inspector.
- `npm run test:e2e` runs the suite headlessly.
- `npm run test:e2e:agent-kernel-gate` is the dedicated seeded browser → API →
  database release gate. Its spec skips under the ordinary mocked suite so an
  unseeded server can never be mistaken for release evidence.
- `npm run test:e2e:headed` runs the suite with a visible browser.
- `npm run test:e2e:debug` opens Playwright Inspector.
- `npm run test:e2e:report` opens the HTML report after a run.

If `BASE_URL` is unset, Playwright targets `http://127.0.0.1:4173`.
Use `npm run test:e2e:local` for the local one-command flow, or set `BASE_URL` when targeting an already running environment.

## Architecture

- `tests/e2e/` holds user-facing browser specs.
- `tests/support/helpers/` holds route stubs and other cross-test helpers.
- `tests/support/fixtures/` wires factories and boot helpers into the Playwright `test` object.
- `tests/support/fixtures/factories/` creates deterministic graph data without hard-coding every task payload.
- `tests/support/page-objects/` is reserved for future UI abstractions when flows become repetitive enough to justify them.

## Best Practices

- Stub network routes before `page.goto()` so boot-time fetches stay deterministic.
- Prefer `data-testid` for shell-level selectors and semantic roles for user-visible controls.
- Retired compatibility proposal-card scenarios must be explicitly skipped with
  a native ReviewDocument replacement named in the reason. Keep one fail-closed
  browser contract proving that retired cards cannot approve or mutate data.
- Keep factories in charge of object shape so new tests do not drift from the API contract.
- Use one scenario per spec where possible and let fixtures own setup and cleanup.
- Avoid hard waits. Use response waits and visible UI assertions instead.

## CI Notes

- The config emits list, HTML, and JUnit reporters.
- Artifacts write to `test-results/` and `playwright-report/`.
- CI should run `npm install`, `npm run test:e2e:install`, and then either `npm run test:e2e:local` or a separately started app server plus `npm run test:e2e`.

## Knowledge References

This scaffold follows the project TEA patterns for fixture architecture, data factories, network-first setup, Playwright config, and test quality.

## Troubleshooting

- If the browser is missing, run `npm run test:e2e:install`.
- If the app never boots, confirm the local port in `.env` matches `BASE_URL`, or use `npm run test:e2e:local` to let the helper manage startup.
- If a new test needs mutations, extend the route helper to mock the relevant POST/PATCH/DELETE endpoints before reaching for a real backend.
