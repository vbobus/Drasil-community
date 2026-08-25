# Deployment and multi-user setup

Drasil supports two layouts with the same application build:

1. **One machine:** a single container serves the frontend and backend at one
   address. This is the easiest and safest default.
2. **Split frontend and backend:** the backend runs on a VPS, home server, or
   another machine while a browser loads the frontend elsewhere and connects to
   the backend's HTTPS address.

Accounts work the same in both layouts. Each account receives a separate SQLite
database, Codex home, provider-settings file, Google OAuth tokens, checkpoint
database, and remembered preferences. The small account registry contains
usernames, password hashes, and hashed session tokens—not planning data or raw
passwords.

## One-container setup

```sh
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the generated value into `DRASIL_BOOTSTRAP_TOKEN` in `.env`, then start:

```sh
docker compose up --build -d
docker compose ps
```

Open <http://localhost:8000>. Leave the backend address blank, enter the setup
token once, and create the first administrator. Add other accounts under
**Settings → Accounts & server**. Each new account opens an empty workspace with
default categories and can save its own AI provider, API key, Codex login, and
calendar connection.

The Compose file binds to `127.0.0.1` by default. Data is in the `drasil-data`
Docker volume. The image does not contain personal data, environment files,
credentials, private evaluations, or a prebuilt database.

Never set `DRASIL_TEST_ISOLATED_DB=1` in a real deployment. That flag disables
account enforcement only for isolated automated test databases.

The standard container supports the OpenAI API provider. The Codex CLI is not
bundled into the image; using the ChatGPT/Codex-login provider in a container
requires a custom image that installs the CLI. Source installs can use a Codex
CLI already installed on the machine.

## Existing single-user installation

On first account creation, Drasil adopts an existing `drasil.db` when
`DRASIL_LEGACY_DATABASE_PATH` points to it. It also copies existing local
provider and Google settings—and an existing Codex `auth.json` when available—
into the first account's private directory. Originals are left in place so the
migration is recoverable. Later accounts always start empty.

Back up the account registry and all account directories together. Restoring
only one side can leave data present but impossible to sign into. For Compose,
back up the complete `drasil-data` volume while Drasil is stopped.

## Backend on a VPS or home server

Keep port 8000 private and put one of these in front of it:

- a Tailscale connection available only to your tailnet;
- an SSH tunnel for occasional personal access; or
- an HTTPS reverse proxy such as Caddy, nginx, or Traefik.

Do not send Drasil passwords or session tokens over plain HTTP across a network.
Account login protects who may read data, while TLS/Tailscale protects the login
and data while they travel between browser and server.

Recommended VPS `.env` values:

```dotenv
DRASIL_BIND_ADDRESS=127.0.0.1
DRASIL_PORT=8000
DRASIL_BOOTSTRAP_TOKEN=
DRASIL_FRONTEND_ORIGIN=https://drasil.example.com
DRASIL_CORS_ORIGINS=https://drasil.example.com
GOOGLE_CALENDAR_REDIRECT_URI=https://api.drasil.example.com/api/v1/calendar-integrations/google/callback
```

Set `DRASIL_BOOTSTRAP_TOKEN` to a random value before the first remote setup.
After the administrator exists, the token is no longer accepted; it may be
removed from the environment. Restrict reverse-proxy request sizes and add a
proxy-level login rate limit if the service is internet-facing. Drasil also has
a small in-process password-guessing brake, but the edge proxy is the better
place for a durable limit across restarts and multiple workers.

## Frontend on a different machine or host

The frontend has no private build-time configuration. Build and serve it as
static files:

```sh
npm ci --prefix frontend
npm --prefix frontend run build
```

Serve `frontend/dist` using any static web server. On its first screen, enter
the backend base address, for example `https://api.drasil.example.com`. The
address is remembered in that browser; the login token is kept only in the
current browser tab. The backend must list the frontend's exact origin in
`DRASIL_CORS_ORIGINS`. Wildcard CORS is intentionally unsupported.

If frontend and backend share the same hostname through a reverse proxy, route
`/api/` to FastAPI and all other paths to the frontend. In that layout, leave
the backend address blank and no CORS customization is needed.

## Google Calendar and local MCP

Google OAuth client credentials are server-wide, while each account's OAuth
tokens and calendar records are isolated. Register the backend callback URL
exactly as `GOOGLE_CALENDAR_REDIRECT_URI`.

The local stdio MCP server must be bound to one account. If the server has more
than one account, set `DRASIL_MCP_ACCOUNT` to that account's username before
starting `python -m app.mcp_server`. This prevents one local MCP process from
silently crossing between users.

## Why remote access is configured this way

The pattern follows the useful parts of OpenClaw's gateway model: local access
defaults to loopback, remote clients specify an explicit server URL, and remote
access is expected to use authentication plus Tailscale, an SSH tunnel, or TLS.
See OpenClaw's [gateway](https://github.com/openclaw/openclaw/blob/main/docs/gateway/index.md)
and [security](https://github.com/openclaw/openclaw/blob/main/docs/gateway/security/index.md)
documentation for the analogous deployment boundary.
