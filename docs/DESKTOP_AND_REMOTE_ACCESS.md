# Windows desktop and private remote access

## Recommended architecture

Run one Drasil container on an always-on VPS and expose the complete site only
inside your Tailscale network. Every client uses the same HTTPS origin:

```text
Windows installer ─┐
Desktop browser ───┼── HTTPS over Tailscale ── Drasil container ── account databases
iPhone / Android ──┤
iPad / tablet ─────┘
```

The backend is the single source of truth. There is no second desktop database,
file replication, or client-side merge process. A change made on one device is
written to the same account database that every other device reads.

This layout is intentionally **online-first**, not offline-first. An offline
client cannot safely edit and later merge planning and calendar data yet.

## Why this layout fits Drasil

- The existing container already serves the React frontend and FastAPI API from
  one origin, so browser CORS configuration is unnecessary.
- Tailscale Serve gives the tailnet a valid HTTPS address while the container
  remains bound to `127.0.0.1`; port 8000 is not exposed to the public internet.
- Drasil login remains required. Tailscale controls which devices can reach the
  server, while Drasil controls which account data an authenticated user sees.
- The Electron app is a small, replaceable shell. Most UI releases are deployed
  once to the VPS and appear on every device without rebuilding an installer.
- SQLite remains appropriate for one personal server with one application
  process. PostgreSQL is unnecessary until the product needs multiple backend
  replicas or materially higher write concurrency.

## 1. Start Drasil on a VPS

Install Docker and Tailscale on a small Linux VPS, join the VPS to your tailnet,
then clone the repository. Keep the default loopback binding.

```sh
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the generated value in `DRASIL_BOOTSTRAP_TOKEN`. If the Tailscale machine is
named `drasil-vps` and the tailnet DNS suffix is `example.ts.net`, use:

```dotenv
DRASIL_BIND_ADDRESS=127.0.0.1
DRASIL_PORT=8000
DRASIL_FRONTEND_ORIGIN=https://drasil-vps.example.ts.net
DRASIL_CORS_ORIGINS=
GOOGLE_CALENDAR_REDIRECT_URI=https://drasil-vps.example.ts.net/api/v1/calendar-integrations/google/callback
```

Start the application and publish its loopback port to the tailnet:

```sh
docker compose up --build -d
docker compose ps
tailscale serve --bg http://127.0.0.1:8000
tailscale serve status
```

Open the HTTPS URL printed by `tailscale serve status`. Create the administrator
with the bootstrap token. After setup succeeds, remove
`DRASIL_BOOTSTRAP_TOKEN` from `.env` and recreate the container:

```sh
docker compose up -d --force-recreate
```

Do not use Tailscale Funnel for this personal deployment: Funnel makes the
service public. Tailscale Serve keeps it private to the tailnet and applies
tailnet access-control rules.

## 2. Connect Windows

Build the x64 installer from PowerShell at the repository root:

```powershell
./build-windows.cmd
```

For Windows on ARM:

```powershell
./build-windows.cmd -Architecture arm64
```

The script prints the full path to `DrasilSetup.exe` under `desktop/out/make`.
Install it, start Drasil, and enter the same Tailscale HTTPS URL. The choice is
stored in Electron's per-user application-data directory. Use **Drasil → Change
server…** to replace it.

The shell also accepts a one-time command-line address for development:

```powershell
Drasil.exe --server=https://drasil-vps.example.ts.net
```

Remote content runs with Node integration disabled, context isolation and the
Chromium sandbox enabled, web security enabled, permission requests denied, and
navigation restricted to the configured origin. External HTTPS links open in
the normal browser. Plain HTTP is accepted only for `localhost` development.

The local installer is unsigned, so Windows may display an unknown-publisher
warning. Public distribution should add an Authenticode certificate and sign
both the executable and installer. Code signing is independent of the public
Git publishing key.

## 3. Connect phone and iPad

1. Install Tailscale on the device and join the same tailnet.
2. Open the Drasil Tailscale HTTPS URL in Safari, Chrome, or Firefox.
3. Sign in to the same Drasil account.
4. Optionally use **Add to Home Screen** / **Install app**. Drasil publishes a
   web-app manifest and uses a standalone layout when the browser supports it.

At phone and tablet widths, Drasil shows touch-sized **Plan**, **AI**, and
**Calendar** view buttons. Each view receives the full available workspace.
The calendar remains interactive on narrow screens and scrolls its week/month
surfaces inside the panel instead of hiding them.

The installed web app still requires connectivity to the VPS. Tailscale must be
connected when the address is private to the tailnet.

## Updates and backups

Update the VPS first:

```sh
git pull --ff-only
docker compose up --build -d
docker compose ps
```

That updates the browser UI, phone/tablet UI, API, and the web content displayed
inside Electron. Rebuild `DrasilSetup.exe` only when files under `desktop/`
change or when Electron itself is upgraded.

Back up the complete `drasil-data` Docker volume while the container is stopped.
The account registry and all account directories must be restored together.
Keep at least one encrypted off-VPS copy and test a restore periodically.

## When to choose a public HTTPS domain instead

Use Caddy/nginx and a normal domain only if clients cannot run Tailscale or the
application must be reachable without joining the tailnet. In that model, keep
port 8000 on loopback, terminate TLS at the reverse proxy, add durable edge rate
limiting, monitor authentication attempts, and maintain timely security updates.
Drasil login alone is not a substitute for TLS or an access-control edge.

## Current boundaries

- No offline write queue or conflict resolution.
- One authoritative backend instance; do not run two containers against copied
  SQLite files and expect them to synchronize.
- Desktop sessions and browser sessions sign in independently.
- Tailscale availability is required for the recommended private deployment.
- Public installers should be code-signed before distribution to other people.

References: [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve),
[Tailscale on iOS](https://tailscale.com/docs/install/ios), and
[Electron security](https://www.electronjs.org/docs/latest/tutorial/security).
