# AI 官方状态监控（OpenAI / Claude / Gemini / Grok / AWS）

## 运行方式

```bash
python3 monitor.py run
python3 monitor.py loop --interval 60
python3 monitor.py report --day 2026-06-18
```

## 输出

- `output/last_run.json`：本次采样快照
- `output/events.ndjson`：采样 + 告警事件追加日志
- `reports/daily-report-YYYY-MM-DD.md|json`：日报（含 24h 统计与最近事件）
- `public/index.html`：可直接放到 Pages 的只读页

## 规则（按你的计划实现）

- 分析源：
  - OpenAI：`status.openai.com/api/v2/summary.json`
  - Claude：`status.claude.com/api/v2/summary.json`
  - Gemini：`status.cloud.google.com/incidents.json`
  - Grok：`status.x.ai/` + `status.x.ai/feed.xml`
  - AWS：`health.aws.amazon.com/public/currentevents`（UTF-16 解码）
- 告警判定：
  - `minor/degraded_performance/partial_outage` → `warn`
  - `major/major_outage/incident ongoing` → `critical`
  - 连续 2 次异常才告警；连续 2 次恢复才消警
- 存储：
  - `data/monitor.db`（SQLite）：`runs/ incidents / service_state / alerts`
  - 输出 JSON/NDJSON 同时保留，便于 Pages/审计复用

## 文件
- `services.json`：可扩展服务清单
- `monitor.py`：抓取、评分、告警、存储、报表核心
