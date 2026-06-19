# Security Policy

## Scope

This project fetches public official status feeds and generates static files. It should not require API keys, private credentials, or secrets.

## Supported Branch

Security fixes are made on `main`.

## Reporting

If you find a security issue, open a GitHub issue with minimal reproduction details, or contact the maintainer through the GitHub profile if the report should not be public.

Do not include credentials, private tokens, or internal service URLs in public issues.

## Design Notes

- The monitor intentionally avoids authenticated provider APIs.
- GitHub Actions uses the repository-provided `GITHUB_TOKEN` only to publish Pages output.
- Generated runtime state is published to `gh-pages`; do not put secrets in `services.json`.
