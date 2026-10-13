# Host on Vercel and Neon

This layout serves React and FastAPI from one Vercel project and stores accounts in PostgreSQL. It starts with empty accounts; no local databases, credentials, or private deployment configuration are included in Community.

## Prepare the project

1. Fork or clone Drasil Community. Create a Vercel project from your fork with the repository root as its root directory. Keep the checked-in `vercel.json`, `index.py`, `.python-version`, and root `requirements.txt`.
2. Create your own Neon PostgreSQL database. Copy its pooled connection string into Vercel's **Production** environment as `DATABASE_URL`. Keep the value private.
3. Choose the final HTTPS domain before configuring the application origin. Use your own project domain or custom domain throughout; the examples below use `https://drasil.example.com`.

The repository's build command installs frontend dependencies, runs TypeScript/Vite with `VITE_DRASIL_HOSTED=1`, and writes browser assets to `public/`. The API entrypoint is `index.py`. The checked-in upload allowlist excludes local data and credentials; do not replace it with an unrestricted upload.

This setup follows [Vercel's FastAPI deployment support](https://vercel.com/docs/frameworks/backend/fastapi). Neon explains [pooled connection strings](https://neon.com/docs/connect/connection-pooling). Drasil's storage code applies certificate/hostname verification and a transaction-local account namespace; TLS credentials remain server-side.

## Set private environment variables

Generate a bootstrap secret and encryption key on your machine:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the outputs into Vercel's environment settings, not a committed file.

| Variable | Value |
| --- | --- |
| `DRASIL_STORAGE` | `postgres` |
| `DATABASE_URL` | Your private pooled PostgreSQL connection string |
| `DRASIL_FRONTEND_ORIGIN` | Your exact HTTPS origin, without a trailing slash |
| `DRASIL_BOOTSTRAP_TOKEN` | Generated first-account secret |
| `DRASIL_ENCRYPTION_KEY` | Generated Fernet key |
| `DRASIL_SESSION_LIFETIME_SECONDS` | `604800`, or your chosen lifetime |
| `DRASIL_TIMEZONE` | Your IANA timezone, for example `UTC` |
| `STRATEGIST_PROVIDER` | `openai-api` for an API connection, or `codex-cli` with the optional Sandbox setup below |

Vercel supplies `VERCEL=1`. Never set `DRASIL_TEST_ISOLATED_DB=1`; hosted Drasil rejects that configuration. Never put secrets in a variable starting with `VITE_`, because those values can enter the browser bundle.

Keep a secure backup of `DRASIL_ENCRYPTION_KEY` alongside database backups. Replacing it without re-encrypting stored values makes existing integration credentials unreadable.

## Deploy and create the first account

Deploy from Vercel after saving the production environment. Or use the CLI from a clean, reviewed checkout:

```sh
npx vercel login
npx vercel link
npx vercel deploy --prod
```

Visit your own HTTPS origin, pass any Vercel deployment protection you enabled, and create the administrator using your private setup token. Leave the backend address blank. After the first account exists, public bootstrap closes; the administrator can add accounts in Settings. You may remove the bootstrap environment value and redeploy once setup is complete.

Check `/api/v1/health`, sign-in, task saving, calendar loading, and Habits. Anonymous reads of `/api/v1/workspace` and `/api/v1/habits` should return HTTP 401. Do not run automated test-bypass fixtures against production.

## Optional AI and calendar connections

Planning works without AI. Users can configure their own OpenAI API key in Settings; it is encrypted in their account's private settings. API usage is separately billed.

Hosted ChatGPT/Codex login additionally needs:

- `DRASIL_CODEX_SANDBOX=1` and `STRATEGIST_PROVIDER=codex-cli`;
- Vercel **Secure Backend Access / OIDC** enabled for the project;
- an account that permits Codex device-code sign-in.

Then use **Settings > Connections > Continue with ChatGPT** and complete the displayed OpenAI sign-in flow. Each Drasil account has a private Sandbox and Codex home. Do not copy an owner's local authentication file into the repository or a deployment. See [Vercel Sandbox authentication](https://vercel.com/docs/sandbox) and [Vercel OIDC](https://vercel.com/docs/oidc) for project prerequisites. Sandbox usage and model/subscription limits apply; this guide does not promise unlimited free operation. Missing subscription access never falls back automatically to paid API use.

Google Calendar needs your own OAuth client, consent configuration, and:

```dotenv
GOOGLE_CALENDAR_CLIENT_ID=
GOOGLE_CALENDAR_CLIENT_SECRET=
GOOGLE_CALENDAR_REDIRECT_URI=https://drasil.example.com/api/v1/calendar-integrations/google/callback
```

Set the client values privately in Vercel and register your exact callback in Google Cloud. The built-in calendar does not require Google sync.

## Preview environments and backups

Give previews a separate database/branch and distinct bootstrap and encryption keys. Do not point an untrusted pull-request preview at personal production data. Avoid pulling production secrets into a checkout used for demos.

Back up PostgreSQL and the encryption key, and test restoration privately. Keep provider tokens, account records, traces, and backups out of Git. Updates initialize current account schema through the application; review migration requirements before deploying future schema changes. SQLite legacy migrations must not be run against PostgreSQL.

The hosted agent runs within the request lifetime. Very long jobs may need smaller requests. Optional persistent companion workers belong on a separate host, not inside a short-lived function. Provider quotas, hosting limits, and costs can change; review the providers' current usage dashboards and plan terms before selecting a plan.
