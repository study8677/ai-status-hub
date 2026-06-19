# Self-hosting

You can fork this repository and run your own official-source AI status monitor without external services.

## Quick Start on GitHub

1. Fork this repository.
2. Enable GitHub Actions in the fork.
3. Enable GitHub Pages from the `gh-pages` branch.
4. Trigger `AI Services Status Monitor` once from the Actions tab.
5. Open your Pages URL.

The hosted workflow runs every 5 minutes:

```yaml
schedule:
  - cron: '*/5 * * * *'
```

GitHub-hosted runners are not reliable for strict 1-minute polling. For 1-minute monitoring, use a self-hosted runner or an external cron.

## Local Cron

```bash
python3 monitor.py run
python3 monitor.py report
```

For repeated local execution:

```bash
python3 monitor.py loop --interval 60
```

## Add a Service

1. Add an entry in `services.json`.
2. Implement a parser in `monitor.py`.
3. Add tests in `tests/test_monitor.py`.
4. Run:

```bash
python3 -m unittest discover -s tests -v
```

## Runtime State

Generated runtime files are intentionally ignored on `main`:

- `data/monitor.db`
- `output/last_run.json`
- `output/events.ndjson`
- `reports/daily-report-*.md`
- `public/index.html`

The GitHub Action restores and publishes these through `gh-pages`.
