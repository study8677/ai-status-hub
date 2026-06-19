from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from monitor import (
    CheckResult,
    ServiceConfig,
    Store,
    evaluate_transition,
    parse_aws_service,
    parse_gemini_service,
    parse_grok_service,
    parse_statuspage_service,
    render_public_page,
    write_last_run,
)


SAMPLE_TIME = datetime(2026, 6, 19, 2, 0, tzinfo=timezone.utc)


def service(name: str, kind: str = "statuspage") -> ServiceConfig:
    return ServiceConfig(
        name=name,
        kind=kind,
        summary_url=f"https://example.com/{name}",
        source_url=f"https://example.com/{name}",
    )


class MonitorParserTests(unittest.TestCase):
    def sample_result(self) -> CheckResult:
        return CheckResult(
            service="Gemini",
            time="2026-06-19T02:00:00Z",
            overall_status="operational",
            components=[],
            active_incidents=[],
            raw_score=100,
            confidence=0.95,
            updated_at="2026-06-19T02:00:00Z",
            source_url="https://status.cloud.google.com",
            level="ok",
        )

    def test_statuspage_none_indicator_is_ok(self) -> None:
        payload = {
            "page": {"name": "Claude", "updated_at": "2026-06-19T01:00:00Z"},
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "components": [{"name": "API", "status": "operational"}],
            "incidents": [],
        }

        result = parse_statuspage_service(json.dumps(payload).encode(), service("Claude"), SAMPLE_TIME)

        self.assertEqual(result.level, "ok")
        self.assertEqual(result.raw_score, 100)

    def test_statuspage_major_active_incident_is_critical(self) -> None:
        payload = {
            "page": {"name": "Claude", "updated_at": "2026-06-19T01:00:00Z"},
            "status": {"indicator": "none"},
            "components": [{"name": "API", "status": "operational"}],
            "incidents": [
                {
                    "id": "inc-1",
                    "name": "API outage",
                    "status": "investigating",
                    "impact": "major",
                    "created_at": "2026-06-19T00:00:00Z",
                    "updated_at": "2026-06-19T00:30:00Z",
                    "shortlink": "https://example.com/inc-1",
                }
            ],
        }

        result = parse_statuspage_service(json.dumps(payload).encode(), service("Claude"), SAMPLE_TIME)

        self.assertEqual(result.level, "critical")
        self.assertEqual(result.latest_incident_link, "https://example.com/inc-1")

    def test_gemini_filter_only_keeps_gemini_incidents(self) -> None:
        payload = [
            {
                "id": "network-1",
                "affected_products": [{"title": "Virtual Private Cloud"}],
                "status_impact": "SERVICE_DISRUPTION",
                "severity": "medium",
                "begin": "2026-06-19T00:00:00+00:00",
                "uri": "incidents/network-1",
            },
            {
                "id": "gemini-1",
                "affected_products": [{"title": "Vertex AI Gemini API"}],
                "status_impact": "SERVICE_DISRUPTION",
                "severity": "high",
                "begin": "2026-06-19T00:10:00+00:00",
                "uri": "incidents/gemini-1",
            },
        ]

        result = parse_gemini_service(json.dumps(payload).encode(), service("Gemini", "gemini"), SAMPLE_TIME)

        self.assertEqual(result.level, "critical")
        self.assertEqual(len(result.active_incidents), 1)
        self.assertEqual(result.active_incidents[0]["id"], "incidents/gemini-1")

    def test_grok_resolved_rss_with_blocked_page_is_ok(self) -> None:
        feed = """<?xml version="1.0" encoding="UTF-8" ?>
        <rss version="2.0">
          <channel>
            <lastBuildDate>Wed, 17 Jun 2026 16:43:50 GMT</lastBuildDate>
            <item>
              <title>[API] Increased Error rate</title>
              <description><![CDATA[<h3>Status: RESOLVED</h3><p>Severity: available</p>]]></description>
              <pubDate>Wed, 17 Jun 2026 12:13:15 GMT</pubDate>
              <category>available</category>
              <category>resolved</category>
            </item>
          </channel>
        </rss>
        """
        blocked_page = b"<html><title>Attention Required! | Cloudflare</title></html>"

        result = parse_grok_service(blocked_page, feed.encode(), service("Grok", "grok"), SAMPLE_TIME)

        self.assertEqual(result.level, "ok")
        self.assertEqual(result.active_incidents, [])

    def test_aws_utf16_current_event_uses_event_timestamp_and_status_level(self) -> None:
        payload = [
            {
                "date": "1772369485",
                "arn": "arn:aws:health:::event/test",
                "region_name": "UAE",
                "status": "3",
                "service": "multipleservices-me-central-1",
                "service_name": "Multiple services",
                "summary": "Increased Error Rates",
                "event_log": [
                    {"summary": "Investigating", "message": "Investigating", "status": 1, "timestamp": 1772369485},
                    {"summary": "Update", "message": "Still impacted", "status": 3, "timestamp": 1772371152},
                ],
            }
        ]

        result = parse_aws_service(json.dumps(payload).encode("utf-16"), service("AWS", "aws"), SAMPLE_TIME)

        self.assertEqual(result.level, "critical")
        self.assertEqual(result.updated_at, "2026-03-01T13:19:12Z")
        self.assertEqual(result.active_incidents[0]["latest_message"], "Still impacted")

    def test_unknown_source_failure_does_not_create_provider_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "monitor.db")
            try:
                result = CheckResult(
                    service="OpenAI",
                    time="2026-06-19T02:00:00Z",
                    overall_status="error",
                    components=[{"name": "Official source", "status": "unreachable"}],
                    active_incidents=[],
                    raw_score=50,
                    confidence=0.3,
                    updated_at="2026-06-19T02:00:00Z",
                    source_url="https://status.openai.com",
                    level="unknown",
                    error="timeout",
                )

                first = evaluate_transition(store, result)
                second = evaluate_transition(store, result)

                self.assertEqual(first, [])
                self.assertEqual(second, [])
                self.assertEqual(result.level, "unknown")
            finally:
                store.close()

    def test_last_run_contains_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            write_last_run(output_dir, [self.sample_result()], [])
            payload = json.loads((output_dir / "last_run.json").read_text(encoding="utf-8"))

            self.assertEqual(payload["schema_version"], "1.0")
            self.assertEqual(payload["services"][0]["service"], "Gemini")

    def test_public_render_writes_seo_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            public_dir = root / "public"

            write_last_run(output_dir, [self.sample_result()], [])
            render_public_page(output_dir / "last_run.json", public_dir)

            html = (public_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("AI Status Hub", html)
            self.assertIn('data-lang-button="en"', html)
            self.assertIn('data-lang-button="zh"', html)
            self.assertIn("AI 服务官方状态中心", html)
            self.assertIn("ai-status-hub-language", html)
            for name in ["robots.txt", "sitemap.xml", "favicon.svg", "status.svg", "og.svg", "last_run.json", "schema/last_run.schema.json"]:
                self.assertTrue((public_dir / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
