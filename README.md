# AI 服务官方状态监控

[![Monitor](https://github.com/study8677/aistatues/actions/workflows/monitor.yml/badge.svg)](https://github.com/study8677/aistatues/actions/workflows/monitor.yml)
[![Live dashboard](https://img.shields.io/badge/live-dashboard-0b57d0)](https://study8677.github.io/aistatues/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)](https://www.python.org/)
[![Sources](https://img.shields.io/badge/sources-official_only-137333)](#官方数据源)

官方状态源优先的 AI 服务稳定性监控。当前覆盖 OpenAI、Claude、Gemini、Grok、AWS，使用 GitHub Actions 托管轮询，并发布中文只读看板。

**在线看板：** https://study8677.github.io/aistatues/

## 更新频率

- 计划频率：每 5 分钟触发一次。
- 触发分钟：北京时间每小时 `00 / 05 / 10 / 15 / 20 / 25 / 30 / 35 / 40 / 45 / 50 / 55` 分。
- 实际更新：GitHub Actions 触发后还要完成采样、生成页面、推送 `gh-pages`，通常会晚几十秒到数分钟。
- 限制说明：GitHub 托管 runner 的 schedule 不适合做严格 1 分钟轮询；如果要 1 分钟级别，需要自托管 runner 或外部调度器。

## 看板会显示什么

- 每个服务的 `正常 / 预警 / 严重 / 未知`
- 官方 incident、影响组件、源更新时间、采样时间、异常持续时间
- 24 小时日报和原始 JSON/NDJSON 快照
- 去抖动告警状态：连续 2 次异常才触发，连续 2 次恢复才消警

## 官方数据源

| 服务 | 官方源 | 解析方式 | 说明 |
|---|---|---|---|
| OpenAI | https://status.openai.com/api/v2/summary.json | Statuspage JSON | 读取组件状态和 active incident。 |
| Claude | https://status.claude.com/api/v2/summary.json | Statuspage JSON | `indicator=none` 视为基础正常，但 active major incident 仍会拉红。 |
| Gemini | https://status.cloud.google.com/incidents.json | Google Cloud incident JSON | 只筛选 Gemini / Vertex Gemini 相关事件。 |
| Grok | https://status.x.ai/feed.xml | xAI RSS + 页面回退 | 页面被 Cloudflare 拦截时，以 RSS 作为主要机器可读源。 |
| AWS | https://health.aws.amazon.com/public/currentevents | AWS public health JSON | 全量 AWS public current events；响应为 UTF-16 JSON，事件时间来自 `event_log`。 |

## 托管运行

- Workflow：[`monitor.yml`](./.github/workflows/monitor.yml)
- 调度：`*/5 * * * *`
- 运行环境：Python 3.12，无第三方依赖
- 发布目标：`gh-pages` 分支

## 输出

由 `gh-pages` 分支发布：

- 看板：https://study8677.github.io/aistatues/
- 最新 JSON：https://study8677.github.io/aistatues/last_run.json
- 原始快照：https://study8677.github.io/aistatues/output/last_run.json
- 事件日志：https://study8677.github.io/aistatues/output/events.ndjson
- 日报：https://github.com/study8677/aistatues/tree/gh-pages/reports

主分支只保留源码、测试、配置和文档；运行态数据由 GitHub Actions 发布到 `gh-pages`。

## 状态规则

| 等级 | 含义 |
|---|---|
| `正常` | 官方源显示 operational，且没有相关 active incident。 |
| `预警` | 降级、部分故障或较低级别 active incident。 |
| `严重` | major outage、service disruption、active major incident，或 AWS public currentevents 里的最高活动影响级别。 |
| `未知` | 官方源抓取或解析失败。这代表监控源健康问题，不直接算作服务故障。 |

## 本地运行

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py run
python3 monitor.py report
python3 monitor.py loop --interval 60
```

## 边界

这个项目只使用官方状态源，不主动探测模型 API，不发送测试 prompt，不依赖第三方聚合状态页。这样可以避免消耗 API quota，也能把信号限定在服务商正式发布的事件上。
