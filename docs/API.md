# Public Output API

The GitHub Pages site publishes static machine-readable files. They are useful for dashboards, automation, and simple integrations.

Base URL:

```text
https://study8677.github.io/aistatues/
```

## Latest Snapshot

```text
GET /last_run.json
GET /output/last_run.json
```

Core fields:

| Field | Meaning |
|---|---|
| `schema_version` | Public snapshot contract version. Current version: `1.0`. |
| `generated_at` | Snapshot generation time in UTC. |
| `services[]` | Normalized service status rows. |
| `alerts[]` | Debounced alert events generated during the run. |

Service row fields:

| Field | Meaning |
|---|---|
| `service` | Service name. |
| `level` | `ok`, `warn`, `critical`, or `unknown`. |
| `overall_status` | Provider-specific normalized status text. |
| `raw_score` | 100 for OK, 70 for warn, 20 for critical, 50 for unknown. |
| `confidence` | Parser confidence in the current source. |
| `active_incidents` | Official incidents still considered active. |
| `source_url` | Official source page. |

Example:

```bash
curl -s https://study8677.github.io/aistatues/last_run.json \
  | jq -r '.services[] | [.service, .level, .overall_status] | @tsv'
```

Schema:

```text
GET /schema/last_run.schema.json
```

The public files are static GitHub Pages assets. They can be fetched directly from browsers or server-side scripts. Cache freshness follows GitHub Pages/CDN behavior; the monitor itself runs every 5 minutes, but visible propagation may lag by seconds to minutes.

## Event Stream

```text
GET /output/events.ndjson
```

Append-only NDJSON for samples and alert transitions.

## Daily Reports

```text
GET /reports/daily-report-YYYY-MM-DD.json
GET /reports/daily-report-YYYY-MM-DD.md
```

Reports summarize the last 24 hours from local SQLite state restored through the `gh-pages` branch.
