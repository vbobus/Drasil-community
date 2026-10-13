# Run Drasil

Choose a persistent Docker server for SQLite, or [Vercel + Neon](HOSTING.md) for hosted PostgreSQL. A static frontend alone cannot store your account data.

## Docker on a server

Clone the Community repository and copy `.env.example` to your private `.env`. Generate a setup token:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set `DRASIL_BOOTSTRAP_TOKEN` to that value. Keep `DRASIL_BIND_ADDRESS=127.0.0.1` and `DRASIL_PORT=8000` when an HTTPS proxy runs on the same host.

```sh
docker compose up --build -d
docker compose ps
```

Open the local address or your configured HTTPS origin. Leave the backend address blank and create the first administrator with the setup token. Later accounts are created by an administrator in **Settings > Accounts & server**; they start empty.

The `drasil-data` volume contains the account registry and each account's SQLite databases and private settings. Never put this volume, `.env`, provider settings, or OAuth tokens in Git or a public image.

The standard image supports an explicitly configured OpenAI API connection. It does not include the Codex CLI. Subscription login needs a source install with Codex installed, or a custom container with the CLI and persistent per-account Codex homes.

## Remote access

Keep port 8000 private. Put an HTTPS reverse proxy or a private Tailscale connection in front of it. Do not send account passwords or sessions over network HTTP.

For a host already joined to Tailscale:

```sh
tailscale serve --bg http://127.0.0.1:8000
tailscale serve status
```

Use the printed HTTPS address on devices in the same tailnet. Set `DRASIL_FRONTEND_ORIGIN` to your own address. With one origin, `DRASIL_CORS_ORIGINS` stays blank. Tailscale Funnel is a different, public exposure option and is not part of this private setup. See [desktop and remote access](DESKTOP_AND_REMOTE_ACCESS.md) for the complete workflow.

For a public reverse proxy, set your own HTTPS origin, apply request-size and login-rate limits, and proxy `/api/` to the backend. Protect the host, keep dependencies updated, and avoid exposing its direct port.

## Separate frontend

```sh
npm ci --prefix frontend
npm --prefix frontend run build
```

Serve `frontend/dist` on your static host. On the first screen, enter the backend base URL, such as `https://api.drasil.example.com`. Configure that frontend's exact HTTPS origin in the backend's `DRASIL_CORS_ORIGINS`; wildcard credentialed CORS is unsupported. An HTTPS frontend requires an HTTPS backend.

When both share a reverse-proxy origin, route `/api/` to FastAPI and everything else to the frontend, and leave the backend address blank. For Vercel's single-project layout, follow [the hosted guide](HOSTING.md) instead.

## AI, calendars, and tracing

AI is optional. Choose ChatGPT/Codex or an OpenAI API connection in Settings; API calls are separately billed. Never paste tokens into a URL or commit them.

Google Calendar requires your own OAuth client, consent-screen configuration, and an exact callback URL, for example `https://drasil.example.com/api/v1/calendar-integrations/google/callback`. Client settings are server-wide; tokens and calendar records belong to their account. Drasil's calendar works without Google sync.

Langfuse tracing is optional. Leave its keys blank to disable it. Aggregate mode excludes application content; full mode exports prompts, responses, and tool payloads to the configured tracing service. Choose deliberately before enabling full mode.

Never enable `DRASIL_TEST_ISOLATED_DB=1` in a real deployment: it is an automated-test bypass, not a server configuration option.

## Backups and upgrades

Back up the complete `drasil-data` volume while the service is stopped. Keep the registry and account directories together; recovering just one side can leave data inaccessible. Protect the backup as private data.

```sh
docker compose down
# Make and verify your private volume backup here.
git pull --ff-only
docker compose up --build -d
```

Do not run `docker compose down -v` unless you intend to delete the data volume. Check sign-in, a task, a calendar view, and Habits after updating.

Existing single-user SQLite installations can be adopted by the first account through `DRASIL_LEGACY_DATABASE_PATH`; back up first. Subsequent accounts always start empty. Hosted PostgreSQL starts separately and does not import your local files.
