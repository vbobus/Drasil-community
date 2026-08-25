# Drasil

Drasil is a local-first planning workspace for turning goals into structured
tasks and realistic schedules. It combines a hierarchical planning tree,
calendar blocks, recurring schedules, preference memory, and reviewable AI
planning workflows in one application.

> Drasil is under active development. Back up local data before upgrading and
> review AI-generated changes before applying them.

## What is included

- Goal and task trees with categories, priorities, dependencies, and estimates
- Calendar planning, recurrence rules, and optional Google Calendar connection
- Constraint-based scheduling backed by OR-Tools
- Strategist and planner workflows with explicit review before writes
- A local SQLite data store that works without a hosted database
- Multiple sign-in accounts with a separate database and private configuration for each person
- A FastAPI backend and React/TypeScript frontend

The public repository contains source code and source tests only. It does not
contain personal databases, credentials, environment files, BMAD material,
private evaluation suites or fixtures, evaluation output, browser traces, or
generated test artifacts.

## Requirements

- Python 3.14
- Node.js 24 and npm
- Optional: Codex CLI, or an OpenAI API key, for AI strategist features
- Optional: Google OAuth credentials for calendar synchronization

## Quick start

The shortest production-style setup is one container serving both the API and
web app:

```sh
cp .env.example .env
# Set DRASIL_BOOTSTRAP_TOKEN in .env to a long random value.
docker compose up --build -d
```

Open <http://localhost:8000>, create the first administrator account, then add
other people under **Settings → Accounts & server**. The named Docker volume
holds the account registry and every account's private data. The default port
binding is loopback-only, so it is reachable only from the same machine.

For source development on Windows, clone the repository and run:

```powershell
Copy-Item backend/.env.example backend/.env
./start.ps1
```

The script creates `backend/venv`, installs dependencies, and opens the backend
and frontend development servers.

For macOS or Linux:

```sh
python -m venv backend/venv
source backend/venv/bin/activate
python -m pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
npm ci --prefix frontend
```

Then run these commands in separate terminals:

```sh
cd backend
./venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```sh
cd frontend
npm run dev
```

Open <http://localhost:5173>. API documentation is available at
<http://127.0.0.1:8000/docs>.

No database download or initialization command is needed. The first account
adopts an existing legacy `backend/drasil.db` when one is present. Every later
account receives a new database seeded only with built-in categories. Database,
login-registry, OAuth, and provider-settings files remain ignored by Git.

## Accounts and deployment

Every API request is authenticated. Each account has a physically separate
SQLite database and private configuration directory, so a missing application
query filter cannot cross user data. The sidebar account button switches users;
administrators can add accounts and users can change their own passwords from
Settings.

For a one-machine install, leave the frontend's backend field blank: browser and
API use the same origin. For a split install, serve the frontend anywhere and
enter the backend's HTTPS address on the sign-in screen. Configure that backend
with the frontend's exact origin in `DRASIL_CORS_ORIGINS`.

See [Deployment and multi-user setup](docs/DEPLOYMENT.md) for Docker, VPS,
Tailscale, reverse-proxy, backup, and separate-frontend instructions.

## Configuration

Copy `backend/.env.example` to `backend/.env`, then change only the options you
need. The default uses a locally authenticated Codex CLI. To use the OpenAI API,
set `STRATEGIST_PROVIDER=openai-api` and provide `OPENAI_API_KEY` in your local
`.env` file or through your environment.

Never commit `.env`, `.secrets`, database, credential, or provider-settings
files. See [SECURITY.md](SECURITY.md) for reporting guidance.

## Development checks

Backend:

```sh
python -m pip install -r backend/requirements-dev.txt
ruff check backend/app
python -m unittest discover -s backend/tests -p "test_*.py"
pip-audit -r backend/requirements.txt
```

Frontend:

```sh
npm ci --prefix frontend
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run test:e2e:local
npm --prefix frontend audit --audit-level=high
```

Set `DRASIL_TEST_ISOLATED_DB=1` when invoking backend tests from unusual test
runners. The standard test commands and CI already isolate test databases.

## Repository layout

```text
backend/app/       FastAPI routes, domain models, and application services
backend/tests/     Backend unit and integration tests
frontend/src/      React application and API client
frontend/tests/    Playwright end-to-end tests
docs/DEPLOYMENT.md Multi-user, container, and remote-access setup
scripts/           Public export and repository-safety checks
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
