# Contributing

欢迎贡献新的官方状态源、解析器、页面改进和测试用例。这个项目的核心原则是：优先使用官方、公开、可机器读取的数据源，避免把网络抖动或第三方聚合误判成服务事故。

## 开发流程

1. Fork 仓库并创建分支。
2. 修改 `services.json` 或 `monitor.py`。
3. 为新解析逻辑补充 `tests/test_monitor.py`。
4. 本地运行：

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py run
python3 monitor.py report
```

5. 提交 Pull Request，并说明使用的数据源和误报边界。

## 接收标准

- 数据源必须是官方源，或明确标注为官方公开 RSS/page fallback。
- 新服务需要输出统一字段：`service`, `time`, `overall_status`, `components`, `active_incidents`, `raw_score`, `confidence`, `updated_at`, `source_url`。
- 抓取失败应显示为 `unknown`，不能直接算作 provider outage。
- 对 active incident 的筛选必须尽量降低无关事件污染。
- 页面变更需要保持移动端可读。

## 不接受的方向

- 主动调用付费模型 API 做探测。
- 依赖第三方 status 聚合站作为主信号。
- 把单次网络失败直接升级成服务故障。
- 提交本地运行态数据到 `main` 分支。
