# AI Services Official Status Monitor

[![Monitor](https://github.com/study8677/aistatues/actions/workflows/monitor.yml/badge.svg)](https://github.com/study8677/aistatues/actions/workflows/monitor.yml)
[![Live dashboard](https://img.shields.io/badge/live-dashboard-0b57d0)](https://study8677.github.io/aistatues/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)](https://www.python.org/)
[![Sources](https://img.shields.io/badge/sources-official_only-137333)](#official-sources)

官方状态源优先的 AI 服务稳定性监控。当前覆盖 OpenAI、Claude、Gemini、Grok、AWS，使用 GitHub Actions 托管轮询，并发布一个只读状态看板。

**Live:** https://study8677.github.io/aistatues/

## What It Shows

- 当前每个服务的 `OK / WARN / CRITICAL / UNKNOWN`
- 官方 incident、影响组件、最新更新时间、异常持续时间
- 24 小时日报和原始 JSON/NDJSON 快照
- 去抖动告警状态：连续 2 次异常才触发，连续 2 次恢复才消警

## Official Sources

| Service | Source | Parser | Notes |
|---|---|---|---|
| OpenAI | https://status.openai.com/api/v2/summary.json | Statuspage JSON | Reads component and active incident status. |
| Claude | https://status.claude.com/api/v2/summary.json | Statuspage JSON | Treats `indicator=none` as operational unless active incidents say otherwise. |
| Gemini | https://status.cloud.google.com/incidents.json | Google Cloud incident JSON | Filters only Gemini / Vertex Gemini related incidents. |
| Grok | https://status.x.ai/feed.xml | xAI RSS + page fallback | RSS is the primary machine-readable fallback when the page is Cloudflare-blocked. |
| AWS | https://health.aws.amazon.com/public/currentevents | AWS public health JSON | Full public AWS current events. Response is UTF-16 JSON; event timestamps come from `event_log`. |

## Hosted Operation

The repository runs from GitHub Actions:

- Workflow: [`.github/workflows/monitor.yml`](./.github/workflows/monitor.yml)
- Schedule: every 5 minutes (`*/5 * * * *`)
- Runtime: Python 3.12, no third-party dependencies
- Publish target: `gh-pages` branch

GitHub-hosted scheduled workflows do not support reliable 1-minute cron. For true 1-minute polling, use a self-hosted runner or an external scheduler that calls `python3 monitor.py run`.

## Outputs

Published by the `gh-pages` branch:

- Dashboard: https://study8677.github.io/aistatues/
- Latest JSON: https://study8677.github.io/aistatues/last_run.json
- Raw monitor output: https://study8677.github.io/aistatues/output/last_run.json
- Event log: https://study8677.github.io/aistatues/output/events.ndjson
- Daily reports: https://github.com/study8677/aistatues/tree/gh-pages/reports

Local/generated paths:

| Path | Purpose |
|---|---|
| `monitor.py` | Fetch, normalize, score, debounce, store, and render status data. |
| `services.json` | Extensible official-source service registry. |
| `data/monitor.db` | SQLite state and 30-day rolling history. |
| `output/last_run.json` | Latest normalized snapshot. |
| `output/events.ndjson` | Append-only sample and alert stream. |
| `reports/daily-report-YYYY-MM-DD.md` | 24-hour summary report. |
| `public/index.html` | Static dashboard generated from the latest snapshot. |

Generated files are ignored on `main`; GitHub Actions restores and republishes runtime state through `gh-pages`.

## Status Rules

| Level | Meaning |
|---|---|
| `OK` | Official source reports operational and no active relevant incident. |
| `WARN` | Degradation, partial outage, or lower-severity active incident. |
| `CRITICAL` | Major outage, service disruption, active major incident, or highest active AWS public-event impact observed in `currentevents`. |
| `UNKNOWN` | Official source fetch/parse failed. This is monitor-source health, not provider outage. |

Alert debounce:

- 2 consecutive abnormal samples create an alert.
- 2 consecutive `OK` samples clear an alert.
- Source failures are shown as `UNKNOWN` and do not create provider outage alerts.

## Run Locally

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py run
python3 monitor.py report
python3 monitor.py loop --interval 60
```

## Design Boundaries

This project intentionally uses official status sources only. It does not actively probe model APIs, run synthetic prompts, or depend on third-party status aggregators. That keeps the signal aligned with provider-published incidents and avoids spending API quota.
