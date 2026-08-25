# Contributing to Drasil

Thanks for helping improve Drasil.

## Before you start

- Search existing issues and pull requests before opening a duplicate.
- Use an issue to discuss substantial behavior, schema, or architecture changes.
- Do not include personal databases, credentials, logs, screenshots containing
  private information, generated test output, or internal planning artifacts.
- Keep changes focused and preserve existing behavior unless the change is
  explicitly documented.

## Development workflow

1. Fork the public repository and create a short-lived branch.
2. Follow the setup in `README.md`.
3. Add or update tests for behavior changes.
4. Run the backend tests, Ruff, frontend lint, frontend build, and relevant
   Playwright tests.
5. Open a pull request describing the reason for the change, user-visible
   behavior, verification performed, and any migration concerns.

Generated databases and test artifacts must remain untracked. Run this before
submitting:

```sh
python scripts/verify_public_export.py .
```

By submitting a contribution, you agree that it is licensed under the Apache
License, Version 2.0, and that you have the right to provide it under that
license.
