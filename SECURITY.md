# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or exposed
secret. Use GitHub's **Report a vulnerability** option on the repository's
Security tab so the report and follow-up remain private.

Include the affected version or commit, reproduction steps, impact, and any
suggested mitigation. Do not access data that is not yours, degrade a service,
or publish exploit details before a fix is available.

Maintainers will acknowledge a report as soon as practical, validate the issue,
and coordinate disclosure and remediation with the reporter.

## Supported versions

Security fixes are applied to the latest version on the default branch. Older
commits and private development snapshots are not supported releases.

## Secrets and local data

Drasil stores data locally by default. Environment files, SQLite databases,
OAuth client files, provider settings, logs, and generated test artifacts must
never be committed. If a real credential is committed anywhere, revoke or
rotate it immediately; removing it from the latest commit is not sufficient.

## Multi-user and remote deployments

Drasil authenticates every application API request and assigns each account a
separate database and private configuration directory. Passwords are stored as
scrypt hashes; browser sessions use opaque random tokens whose hashes are kept
in the account registry. Users on the same instance can see the display names
and usernames needed by the account switcher, but not another account's tasks,
connections, provider keys, or OAuth tokens.

Authentication does not encrypt network traffic. Keep the default loopback
binding for local use. Remote deployments must use Tailscale, an SSH tunnel, or
an HTTPS reverse proxy, and must configure exact frontend origins rather than
wildcard CORS. Set a random `DRASIL_BOOTSTRAP_TOKEN` before creating the first
account remotely, then remove it after setup.

Back up the account registry and all account directories as one unit. Do not
publish either one, even if the other is absent.

## Windows desktop shell

The Electron shell accepts remote content only from the HTTPS origin selected
by the user (or loopback HTTP for local development). The renderer has no Node
integration or preload bridge, uses context isolation and Chromium sandboxing,
keeps web security enabled, denies permission requests, and blocks navigation
away from the selected origin. Keep Electron current because the installer also
ships a Chromium runtime. Publicly distributed installers should be
Authenticode-signed; the local development installer is intentionally unsigned.
