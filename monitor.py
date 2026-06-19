from __future__ import annotations

import argparse
import email.utils
import html
import json
import random
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = ROOT_DIR / "data" / "monitor.db"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output"
DEFAULT_REPORT_DIR = ROOT_DIR / "reports"
DEFAULT_PUBLIC_DIR = ROOT_DIR / "public"
DEFAULT_CONFIG = ROOT_DIR / "services.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utciso(ts: Optional[datetime] = None) -> str:
    return (ts or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utcfromtimestamp(ts: Optional[int]) -> datetime:
    return datetime.fromtimestamp(ts or int(time.time()), tz=timezone.utc)


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def level_from_indicator(indicator: str) -> str:
    level = (indicator or "").strip().lower()
    if level in {
        "operational",
        "available",
        "ok",
        "no_issues",
        "resolved",
        "completed",
        "complete",
        "maintenance",
    }:
        return "ok"
    if level in {
        "minor",
        "degraded_performance",
        "service_disruption",
        "partial_outage",
        "partial_degradation",
        "service_information",
        "watching",
        "monitoring",
        "under_maintenance",
    }:
        return "warn"
    if level in {
        "major",
        "major_outage",
        "critical",
        "disruption",
        "outage",
        "service_outage",
        "down",
    }:
        return "critical"
    return "unknown"


def score_from_level(level: str) -> int:
    return {"ok": 100, "warn": 70, "critical": 20, "unknown": 50}.get(level, 50)


def decode_text(payload: bytes) -> str:
    for enc in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return payload.decode(enc)
        except Exception:
            continue
    return payload.decode("utf-8", errors="replace")


def read_json(payload: bytes) -> Any:
    return json.loads(decode_text(payload))


def http_get(url: str, timeout: int = 25, accept_json: bool = False, allow_unverified: bool = False) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIStatusMonitor/1.0)",
        "Accept": "application/json, text/plain, */*" if accept_json else "*/*",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    if allow_unverified:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=context).read()
    return urllib.request.urlopen(req, timeout=timeout).read()


def fetch_with_retry(url: str, timeout: int = 25, retries: int = 3, accept_json: bool = False) -> bytes:
    last_error: Optional[BaseException] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return http_get(url, timeout=timeout, accept_json=accept_json, allow_unverified=("amazonaws.com" in url))
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(8, 1 << (attempt - 1)) + random.uniform(0, 0.35))
            if ("amazonaws.com" in url) and attempt == 1:
                # retry immediately once with TLS cert verification disabled for known AWS endpoint instability
                try:
                    return http_get(url, timeout=timeout, accept_json=accept_json, allow_unverified=True)
                except Exception:
                    pass
    raise RuntimeError(f"fetch failed for {url}: {last_error}")


@dataclass
class ServiceConfig:
    name: str
    kind: str
    summary_url: str
    source_url: str
    feed_url: Optional[str] = None
    enabled: bool = True
    timeout: int = 25
    retries: int = 3


@dataclass
class CheckResult:
    service: str
    time: str
    overall_status: str
    components: List[Dict[str, Any]]
    active_incidents: List[Dict[str, Any]]
    raw_score: int
    confidence: float
    updated_at: str
    source_url: str
    level: str = "unknown"
    error: Optional[str] = None
    first_anomaly_at: Optional[str] = None
    consecutive_anomalies: int = 0
    consecutive_recoveries: int = 0
    anomaly_seconds: Optional[int] = None
    latest_incident_link: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["time"] = self.time
        return payload


def _coerce_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _incident_is_active(incident: Dict[str, Any]) -> bool:
    ended = incident.get("resolved_at") or incident.get("end") or incident.get("resolved") or incident.get("closed_at")
    if ended:
        return False
    status = str(incident.get("status") or incident.get("state") or incident.get("status_impact") or "").lower()
    if status in {"resolved", "closed", "complete", "completed"}:
        return False
    return True


def _normalize_status_from_feed_text(text: str) -> str:
    t = (text or "").lower()
    if "major outage" in t or "service disruption" in t or "outage" in t:
        return "critical"
    if "degraded" in t or "intermittent" in t or "incident" in t:
        return "warn"
    return "ok"


def _unique_components(components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for comp in components:
        key = (comp.get("name"), comp.get("status"))
        if key in seen:
            continue
        seen.add(key)
        result.append(comp)
    return result


def parse_statuspage_service(raw: bytes, config: ServiceConfig, sample_time: datetime) -> CheckResult:
    data = read_json(raw)
    status_payload = data.get("status") or {}
    indicator = (status_payload.get("indicator") or "unknown").lower()
    overall_level = level_from_indicator(indicator)

    components = [
        {"name": comp.get("name", "Unknown"), "status": comp.get("status", "unknown"), "updated_at": comp.get("updated_at")}
        for comp in _coerce_list(data.get("components"))
    ]

    incidents = []
    for inc in _coerce_list(data.get("incidents")):
        inc_status = str(inc.get("status") or "").lower()
        if inc_status in {"resolved", "closed", "complete", "completed"}:
            continue
        incidents.append(
            {
                "id": inc.get("id"),
                "name": inc.get("name"),
                "status": inc_status or "active",
                "impact": inc.get("impact"),
                "shortlink": inc.get("shortlink") or None,
                "started_at": inc.get("created_at"),
                "updated_at": inc.get("updated_at"),
                "url": inc.get("shortlink") or inc.get("url"),
            }
        )

    for comp in components:
        c_level = level_from_indicator(str(comp.get("status")))
        if c_level == "critical":
            overall_level = "critical"
        elif c_level == "warn" and overall_level == "ok":
            overall_level = "warn"

    for inc in incidents:
        impact = str(inc.get("impact") or "").lower()
        if impact in {"critical", "major", "major_outage", "service_disruption", "service_outage"}:
            overall_level = "critical"
        elif impact in {"minor", "degraded", "partial_outage"} and overall_level == "ok":
            overall_level = "warn"

    page = data.get("page") or {}
    updated_at = page.get("updated_at") or utciso(sample_time)
    latest_link = None
    if incidents:
        latest_ts = None
        latest_inc = incidents[0]
        for inc in incidents:
            dt = parse_datetime(inc.get("updated_at"))
            if dt and (latest_ts is None or dt > latest_ts):
                latest_ts = dt
                latest_inc = inc
        latest_link = latest_inc.get("url") or latest_inc.get("shortlink")

    return CheckResult(
        service=page.get("name") or config.name,
        time=utciso(sample_time),
        overall_status=indicator,
        components=_unique_components(components),
        active_incidents=incidents,
        raw_score=score_from_level(overall_level),
        confidence=0.98,
        updated_at=updated_at,
        source_url=config.source_url,
        level=overall_level,
        latest_incident_link=latest_link,
    )


def parse_gemini_service(raw: bytes, config: ServiceConfig, sample_time: datetime) -> CheckResult:
    data = read_json(raw)
    if isinstance(data, dict):
        incidents = _coerce_list(data.get("incidents"))
        service_updated_at = data.get("updated_at")
        source = data.get("summary", {})
    else:
        incidents = _coerce_list(data)
        service_updated_at = None
        source = {}

    relevant = []
    for incident in incidents:
        affected_products = incident.get("affected_products", [])
        products = []
        if isinstance(affected_products, list):
            products.extend(
                item.get("title", "")
                for item in affected_products
                if isinstance(item, dict) and isinstance(item.get("title"), str)
            )
        service_name = incident.get("service_name")
        if isinstance(service_name, str):
            products.append(service_name)

        is_target = any(
            "gemini" in str(product).lower() or "vertex gemini" in str(product).lower()
            for product in products
        )
        if not is_target:
            continue
        if not _incident_is_active(incident):
            continue

        severity = str(incident.get("severity") or "").lower()
        status_impact = str(incident.get("status_impact") or "").lower()
        derived_level = "warn"
        if status_impact in {"service_disruption", "service_outage"} or severity in {"high", "critical", "major"}:
            derived_level = "critical"

        relevant.append(
            {
                "id": incident.get("uri") or incident.get("id"),
                "name": incident.get("name"),
                "status": status_impact or severity or "service_disruption",
                "impact": incident.get("severity") or incident.get("status_impact"),
                "product_scope": products,
                "link": incident.get("uri"),
                "started_at": incident.get("begin"),
                "updated_at": incident.get("modified") or incident.get("begin"),
                "level": derived_level,
            }
        )

    if relevant:
        overall_level = "warn"
        for item in relevant:
            if item.get("level") == "critical":
                overall_level = "critical"
                break
        latest_inc = relevant[0]
        latest_dt = parse_datetime(latest_inc.get("updated_at"))
        for item in relevant:
            dt = parse_datetime(item.get("updated_at"))
            if dt and (latest_dt is None or dt > latest_dt):
                latest_dt = dt
                latest_inc = item
        latest_link = latest_inc.get("link")
    else:
        overall_level = "ok"
        latest_link = None
        latest_dt = None

    components = []
    for incident in relevant:
        for product in incident.get("product_scope") or []:
            components.append({"name": str(product), "status": incident.get("level")})

    updated_at = source.get("updated_at") if isinstance(source.get("updated_at"), str) else (
        service_updated_at
        if isinstance(service_updated_at, str)
        else (latest_dt.isoformat().replace("+00:00", "Z") if latest_dt else utciso(sample_time))
    )

    return CheckResult(
        service=config.name,
        time=utciso(sample_time),
        overall_status="degraded_performance" if overall_level == "warn" else ("major_outage" if overall_level == "critical" else "operational"),
        components=_unique_components(components),
        active_incidents=relevant,
        raw_score=score_from_level(overall_level),
        confidence=0.95,
        updated_at=updated_at,
        source_url=config.source_url,
        level=overall_level,
        latest_incident_link=latest_link,
    )


def _parse_rfc_datetime(text: str) -> Optional[datetime]:
    if not text:
        return None
    return parse_datetime(text)


def parse_json_like_field(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None

    patterns = [
        r"lastBuildDate\"?\s*[:=]\s*\"([^\"]+)\"",
        r"lastBuildDate:\s*\"([^\"]+)\"",
        r"lastBuildDate\s*=\s*'([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _parse_grok_feed(feed_raw: Optional[bytes]) -> tuple[List[Dict[str, Any]], Optional[datetime]]:
    if not feed_raw:
        return [], None

    feed_text = decode_text(feed_raw)
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError:
        return [], None

    channel_updated = _parse_rfc_datetime((root.findtext(".//lastBuildDate") or "").strip())
    incidents = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = html.unescape((item.findtext("description") or "").strip())
        link = (item.findtext("link") or "").strip() or None
        guid = (item.findtext("guid") or "").strip() or None
        published = _parse_rfc_datetime((item.findtext("pubDate") or "").strip())
        text = f"{title} {desc}".lower()

        is_resolved = "resolved" in text or "available" in text
        if not is_resolved and ("incident" in text or "outage" in text or "error" in text):
            derived = _normalize_status_from_feed_text(text)
            incidents.append(
                {
                    "id": guid,
                    "name": title,
                    "status": "critical" if derived == "critical" else "warn",
                    "severity": derived,
                    "link": link,
                    "updated_at": published.isoformat().replace("+00:00", "Z") if published else None,
                    "raw_status": desc[:120],
                }
            )

    return incidents[:30], channel_updated


def parse_grok_service(
    page_raw: Optional[bytes],
    feed_raw: Optional[bytes],
    config: ServiceConfig,
    sample_time: datetime,
) -> CheckResult:
    incidents, feed_updated = _parse_grok_feed(feed_raw)
    page_text = decode_text(page_raw or b"")

    if incidents:
        overall_level = "critical" if any(item.get("status") == "critical" for item in incidents) else "warn"
    else:
        lowered = page_text.lower()
        if "all systems operational" in lowered or "all systems are operational" in lowered:
            overall_level = "ok"
        elif "major outage" in lowered or ("incident" in lowered and "maintenance" not in lowered):
            overall_level = "critical"
        else:
            overall_level = "warn"
        status_match = parse_json_like_field(page_text)
        if status_match and overall_level != "ok":
            m = status_match.lower()
            if "operational" in m:
                overall_level = "ok"
            elif "major" in m or "outage" in m:
                overall_level = "critical"

    updated = feed_updated
    if not updated:
        updated = parse_datetime(parse_json_like_field(page_text))
    if not updated:
        updated = sample_time

    latest = incidents[0] if incidents else None

    return CheckResult(
        service=config.name,
        time=utciso(sample_time),
        overall_status="operational" if overall_level == "ok" else ("degraded_performance" if overall_level == "warn" else "major_outage"),
        components=[{"name": "xAI System", "status": overall_level}],
        active_incidents=incidents,
        raw_score=score_from_level(overall_level),
        confidence=0.9 if incidents else 0.7,
        updated_at=updated.isoformat().replace("+00:00", "Z") if isinstance(updated, datetime) else utciso(sample_time),
        source_url=config.source_url,
        level=overall_level,
        latest_incident_link=latest.get("link") if isinstance(latest, dict) else None,
    )


def parse_aws_service(raw: bytes, config: ServiceConfig, sample_time: datetime) -> CheckResult:
    data = read_json(raw)
    events = _coerce_list(data)
    active_events = []

    for event in events:
        if not isinstance(event, dict):
            continue
        ended = event.get("end")
        if ended not in (None, "", 0, "0"):
            continue

        summary = str(event.get("summary") or "")
        region = event.get("region_name")
        service_name = event.get("service_name") or event.get("service") or "AWS Service"
        event_status = event.get("status")
        status_text = str(event_status or "").lower()
        severity = "critical" if any(token in summary.lower() for token in ["outage", "disruption", "unavailable"]) else "warn"
        if severity != "critical":
            if any(token in status_text for token in {"1", "2", "warning", "investigating", "maintenance"}):
                severity = "warn"
            if status_text and status_text not in {"1", "2", "3", "4", "5"} and "critical" in status_text:
                severity = "critical"

        active_events.append(
            {
                "id": event.get("arn"),
                "name": f"{service_name} / {region}" if region else service_name,
                "status": event_status or "active",
                "summary": summary,
                "updated_at": event.get("timestamp") or event.get("modified"),
                "link": None,
                "region": region,
                "severity": severity,
            }
        )

    overall_level = "ok"
    if any(evt.get("severity") == "critical" for evt in active_events):
        overall_level = "critical"
    elif active_events:
        overall_level = "warn"

    components = [
        {
            "name": evt.get("name"),
            "status": "degraded" if overall_level in {"warn", "critical"} else "operational",
            "region": evt.get("region"),
        }
        for evt in active_events[:25]
    ]

    updated_values = [parse_datetime(item.get("updated_at")) for item in active_events]
    updated_values = [v for v in updated_values if v]
    updated_at = max(updated_values) if updated_values else sample_time

    return CheckResult(
        service=config.name,
        time=utciso(sample_time),
        overall_status="operational" if overall_level == "ok" else ("degraded_performance" if overall_level == "warn" else "major_outage"),
        components=components,
        active_incidents=active_events[:25],
        raw_score=score_from_level(overall_level),
        confidence=0.97,
        updated_at=updated_at.isoformat().replace("+00:00", "Z"),
        source_url=config.source_url,
        level=overall_level,
        latest_incident_link=None,
    )


def collect_service(config: ServiceConfig) -> CheckResult:
    sample_time = utcnow()
    if config.kind == "statuspage":
        raw = fetch_with_retry(config.summary_url, timeout=config.timeout, retries=config.retries, accept_json=True)
        return parse_statuspage_service(raw, config, sample_time)
    if config.kind == "gemini":
        raw = fetch_with_retry(config.summary_url, timeout=config.timeout, retries=config.retries, accept_json=True)
        return parse_gemini_service(raw, config, sample_time)
    if config.kind == "grok":
        page_raw = None
        try:
            page_raw = fetch_with_retry(config.summary_url, timeout=config.timeout, retries=config.retries)
        except Exception:
            page_raw = None
        feed_raw = None
        if config.feed_url:
            try:
                feed_raw = fetch_with_retry(config.feed_url, timeout=config.timeout, retries=max(1, config.retries - 1))
            except Exception:
                feed_raw = None
        return parse_grok_service(page_raw, feed_raw, config, sample_time)
    if config.kind == "aws":
        raw = fetch_with_retry(config.summary_url, timeout=config.timeout, retries=config.retries, accept_json=False)
        return parse_aws_service(raw, config, sample_time)
    raise ValueError(f"unknown service kind: {config.kind}")


class Store:
    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL,
              service TEXT NOT NULL,
              level TEXT NOT NULL,
              score INTEGER NOT NULL,
              overall_status TEXT,
              confidence REAL NOT NULL,
              raw_score INTEGER NOT NULL,
              updated_at TEXT,
              source_url TEXT,
              payload TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL,
              service TEXT NOT NULL,
              payload TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS service_state (
              service TEXT PRIMARY KEY,
              level TEXT NOT NULL,
              consec_bad INTEGER NOT NULL DEFAULT 0,
              consec_ok INTEGER NOT NULL DEFAULT 0,
              alert_active INTEGER NOT NULL DEFAULT 0,
              first_bad_at INTEGER,
              updated_at INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL,
              service TEXT NOT NULL,
              type TEXT NOT NULL,
              level TEXT NOT NULL,
              message TEXT NOT NULL,
              run_id INTEGER
            )
            """
        )
        self.conn.commit()

    def get_state(self, service: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM service_state WHERE service = ?",
            (service,),
        ).fetchone()
        if row:
            return row
        return {
            "service": service,
            "level": "unknown",
            "consec_bad": 0,
            "consec_ok": 0,
            "alert_active": 0,
            "first_bad_at": None,
            "updated_at": int(time.time()),
        }

    def upsert_state(
        self,
        service: str,
        level: str,
        consec_bad: int,
        consec_ok: int,
        alert_active: int,
        first_bad_at: Optional[int],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO service_state (service, level, consec_bad, consec_ok, alert_active, first_bad_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service) DO UPDATE SET
              level=excluded.level,
              consec_bad=excluded.consec_bad,
              consec_ok=excluded.consec_ok,
              alert_active=excluded.alert_active,
              first_bad_at=excluded.first_bad_at,
              updated_at=excluded.updated_at
            """,
            (service, level, consec_bad, consec_ok, alert_active, first_bad_at, int(time.time())),
        )
        self.conn.commit()

    def save_run(self, result: CheckResult) -> int:
        ts = int(datetime.fromisoformat(result.time.replace("Z", "+00:00")).timestamp())
        cur = self.conn.execute(
            """
            INSERT INTO runs(ts, service, level, score, overall_status, confidence, raw_score, updated_at, source_url, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                result.service,
                result.level,
                result.raw_score,
                result.overall_status,
                result.confidence,
                result.raw_score,
                result.updated_at,
                result.source_url,
                json.dumps(result.to_payload(), ensure_ascii=False),
            ),
        )
        run_id = int(cur.lastrowid)
        for incident in result.active_incidents:
            self.conn.execute(
                "INSERT INTO incidents(run_id, service, payload) VALUES (?, ?, ?)",
                (run_id, result.service, json.dumps(incident, ensure_ascii=False)),
            )
        self.conn.commit()
        return run_id

    def save_alert(self, service: str, alert_type: str, level: str, message: str, run_id: int) -> None:
        self.conn.execute(
            "INSERT INTO alerts(ts, service, type, level, message, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), service, alert_type, level, message, run_id),
        )
        self.conn.commit()

    def prune(self, retention_days: int = 30) -> None:
        cutoff = int((utcnow() - timedelta(days=retention_days)).timestamp())
        self.conn.execute("DELETE FROM runs WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM incidents WHERE run_id NOT IN (SELECT id FROM runs)")
        self.conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def evaluate_transition(store: Store, result: CheckResult) -> List[Dict[str, Any]]:
    state = store.get_state(result.service)
    current_level = state["level"] or "unknown"
    consec_bad = int(state["consec_bad"])
    consec_ok = int(state["consec_ok"])
    alert_active = bool(state["alert_active"])
    first_bad_at = state["first_bad_at"]

    alerts: List[Dict[str, Any]] = []
    observed = result.level
    now_ts = int(datetime.fromisoformat(result.time.replace("Z", "+00:00")).timestamp())

    if observed in {"warn", "critical"}:
        if current_level in {"ok", "unknown"}:
            consec_bad = 1
            first_bad_at = now_ts
        else:
            consec_bad += 1
        consec_ok = 0

        if not alert_active and consec_bad >= 2:
            alerts.append(
                {
                    "type": "alert",
                    "service": result.service,
                    "level": observed,
                    "message": f"{result.service} is in {observed.upper()} state (sampled {result.time})",
                }
            )
            alert_active = True
        elif alert_active and current_level == "warn" and observed == "critical":
            alerts.append(
                {
                    "type": "escalate",
                    "service": result.service,
                    "level": "critical",
                    "message": f"{result.service} escalated to CRITICAL",
                }
            )
    elif observed == "ok":
        if alert_active:
            consec_ok += 1
            if consec_ok >= 2:
                alerts.append(
                    {
                        "type": "recover",
                        "service": result.service,
                        "level": "ok",
                        "message": f"{result.service} recovered",
                    }
                )
                alert_active = False
                first_bad_at = None
        else:
            consec_ok += 1
            if current_level not in {"ok", "unknown"}:
                first_bad_at = None
        consec_bad = 0
    else:
        observed = "warn"
        if current_level in {"ok", "unknown"}:
            consec_bad = 1
            first_bad_at = now_ts
        else:
            consec_bad += 1
        consec_ok = 0
        if not alert_active and consec_bad >= 2:
            alerts.append(
                {
                    "type": "alert",
                    "service": result.service,
                    "level": observed,
                    "message": f"{result.service} is in {observed.upper()} state (sampled {result.time})",
                }
            )
            alert_active = True

    result.level = observed
    result.consecutive_anomalies = consec_bad if observed in {"warn", "critical"} else 0
    result.consecutive_recoveries = consec_ok if observed == "ok" else 0
    result.first_anomaly_at = utcfromtimestamp(first_bad_at).isoformat().replace("+00:00", "Z") if first_bad_at else None
    if first_bad_at and observed in {"warn", "critical"}:
        result.anomaly_seconds = max(0, now_ts - int(first_bad_at))

    store.upsert_state(result.service, observed, consec_bad, consec_ok, int(alert_active), first_bad_at)
    return alerts


def write_last_run(output_dir: Path, results: List[CheckResult], alerts: List[Dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utciso(),
        "services": [result.to_payload() for result in results],
        "alerts": alerts,
    }
    with open(output_dir / "last_run.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def append_events_ndjson(output_dir: Path, results: List[CheckResult], alerts: List[Dict[str, Any]], run_id_map: Dict[str, int]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "events.ndjson"
    with open(path, "a", encoding="utf-8") as fh:
        for result in results:
            row = result.to_payload()
            row["type"] = "sample"
            row["run_id"] = run_id_map.get(result.service)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        for alert in alerts:
            alert_row = {
                "type": "alert",
                "ts": utciso(),
                "service": alert["service"],
                "level": alert["level"],
                "kind": alert["type"],
                "message": alert["message"],
            }
            fh.write(json.dumps(alert_row, ensure_ascii=False) + "\n")


def generate_daily_report(store: Store, report_dir: Path, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = start - timedelta(hours=24)
    report_dir.mkdir(parents=True, exist_ok=True)
    since = int(window_start.timestamp())
    rows = store.conn.execute(
        "SELECT service, ts, level, overall_status, updated_at, payload FROM runs WHERE ts >= ? ORDER BY ts ASC",
        (since,),
    ).fetchall()
    incidents = store.conn.execute(
        """
        SELECT incidents.service, incidents.payload, runs.ts
        FROM incidents
        JOIN runs ON incidents.run_id = runs.id
        WHERE runs.ts >= ?
        ORDER BY runs.ts DESC
        LIMIT 30
        """,
        (since,),
    ).fetchall()

    by_service: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_service[row["service"]].append(row)

    recent_incidents: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in incidents:
        try:
            payload = json.loads(item["payload"])
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        recent_incidents[item["service"]].append(
            {
                "time": utcfromtimestamp(int(item["ts"])).isoformat().replace("+00:00", "Z"),
                "name": payload.get("name"),
                "status": payload.get("status"),
                "link": payload.get("link") or payload.get("url"),
            }
        )

    summary: Dict[str, Any] = {
        "generated_at": utciso(now),
        "window": {
            "start": utcfromtimestamp(since).isoformat().replace("+00:00", "Z"),
            "end": utciso(now),
        },
        "services": {},
    }

    for service, entries in by_service.items():
        total = len(entries)
        level_counts = Counter(entry["level"] for entry in entries)
        ok_count = level_counts.get("ok", 0)
        warn_count = level_counts.get("warn", 0)
        critical_count = level_counts.get("critical", 0)
        latest = entries[-1]
        recent = recent_incidents.get(service, [])
        summary["services"][service] = {
            "total_checks_24h": total,
            "ok": ok_count,
            "warn": warn_count,
            "critical": critical_count,
            "sla": round((ok_count / total) * 100, 2) if total else 100.0,
            "latest_status": latest["level"],
            "latest_at": utcfromtimestamp(int(latest["ts"])).isoformat().replace("+00:00", "Z"),
            "recent_incidents": recent[:30],
        }

    json_path = report_dir / f"daily-report-{start.strftime('%Y-%m-%d')}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    md_lines = [
        f"# Daily status report {start.strftime('%Y-%m-%d')}",
        "",
        "| Service | SLA(OK%) | OK | WARN | CRITICAL | Latest |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for service, item in sorted(summary["services"].items(), key=lambda entry: entry[0]):
        md_lines.append(f"| {service} | {item['sla']:.2f} | {item['ok']} | {item['warn']} | {item['critical']} | {item['latest_status']} |")
    md_lines.append("")
    md_lines.append("## 最近 30 条事件")
    for service, item in sorted(summary["services"].items(), key=lambda entry: entry[0]):
        md_lines.append(f"### {service}")
        service_incidents = item["recent_incidents"][:10]
        if not service_incidents:
            md_lines.append("- 无事件记录")
            continue
        for inc in service_incidents:
            md_lines.append(f"- {inc['time']} | {inc.get('status', '')} | {inc.get('name', '')}")
        md_lines.append("")

    md_path = report_dir / f"daily-report-{start.strftime('%Y-%m-%d')}.md"
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md_lines))

    return summary


def render_public_page(last_run_path: Path, public_dir: Path) -> None:
    if not last_run_path.exists():
        return
    with open(last_run_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    status_rows = []
    for item in data.get("services", []):
        status_rows.append(
            f"""
            <tr>
              <td>{html.escape(item.get('service', ''))}</td>
              <td class='{item.get('level','unknown')}'>{html.escape(item.get('level', 'unknown'))}</td>
              <td>{int(item.get('raw_score', 0))}</td>
              <td>{html.escape(str(item.get('overall_status', '')))}</td>
              <td>{html.escape(str(item.get('updated_at', '')))}</td>
            </tr>
            """
        )

    html_body = f"""<!doctype html>
<html>
  <head>
    <meta charset='utf-8' />
    <title>AI Services Stability Monitor</title>
    <style>
      body {{ font-family: Arial, Helvetica, sans-serif; margin: 24px; }}
      table {{ border-collapse: collapse; width: 100%; max-width: 1100px; }}
      th, td {{ border: 1px solid #ccc; padding: 8px 10px; text-align: left; }}
      .ok {{ color: #1f7a1f; font-weight: 700; }}
      .warn {{ color: #a06d00; font-weight: 700; }}
      .critical {{ color: #aa1f1f; font-weight: 700; }}
      .unknown {{ color: #777; font-weight: 700; }}
    </style>
  </head>
  <body>
    <h1>AI services stability monitor</h1>
    <p>Updated: {html.escape(data.get('generated_at',''))}</p>
    <table>
      <thead>
        <tr><th>Service</th><th>Level</th><th>Score</th><th>Overall status</th><th>UpdatedAt</th></tr>
      </thead>
      <tbody>
        {''.join(status_rows) if status_rows else '<tr><td colspan=\"5\">No data yet</td></tr>'}
      </tbody>
    </table>
  </body>
</html>"""

    public_dir.mkdir(parents=True, exist_ok=True)
    with open(public_dir / "index.html", "w", encoding="utf-8") as fh:
        fh.write(html_body)


def load_services(config_path: Path) -> List[ServiceConfig]:
    with open(config_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    services = []
    for item in raw.get("services", []):
        services.append(
            ServiceConfig(
                name=item["name"],
                kind=item["kind"],
                summary_url=item["summary_url"],
                source_url=item.get("source_url", item["summary_url"]),
                feed_url=item.get("feed_url"),
                enabled=bool(item.get("enabled", True)),
                timeout=int(item.get("timeout", 25)),
                retries=int(item.get("retries", 3)),
            )
        )
    return [service for service in services if service.enabled]


def run_once(store: Store, config_path: Path, output_dir: Path, public_dir: Path) -> Dict[str, Any]:
    services = load_services(config_path)
    now = utciso()
    run_id_map: Dict[str, int] = {}
    results: List[CheckResult] = []
    alerts: List[Dict[str, Any]] = []

    for config in services:
        try:
            result = collect_service(config)
        except urllib.error.HTTPError as exc:
            result = CheckResult(
                service=config.name,
                time=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                overall_status="error",
                components=[],
                active_incidents=[{"message": f"HTTP error: {exc.code} {exc.reason}", "status": "error"}],
                raw_score=20,
                confidence=0.4,
                updated_at=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                source_url=config.source_url,
                level="warn",
                error=f"{exc.code} {exc.reason}",
            )
        except Exception as exc:
            result = CheckResult(
                service=config.name,
                time=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                overall_status="error",
                components=[],
                active_incidents=[{"message": str(exc), "status": "error"}],
                raw_score=20,
                confidence=0.3,
                updated_at=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                source_url=config.source_url,
                level="warn",
                error=str(exc),
            )

        if result.level == "unknown":
            result.level = "warn"
        service_alerts = evaluate_transition(store, result)
        run_id = store.save_run(result)
        run_id_map[result.service] = run_id
        for alert in service_alerts:
            store.save_alert(alert["service"], alert["type"], alert["level"], alert["message"], run_id)
        alerts.extend(service_alerts)
        results.append(result)

    write_last_run(output_dir, results, alerts)
    append_events_ndjson(output_dir, results, alerts, run_id_map)
    generate_daily_report(store, DEFAULT_REPORT_DIR, utcnow())
    render_public_page(DEFAULT_OUTPUT_DIR / "last_run.json", public_dir)
    store.prune(30)
    return {"generated_at": now, "services": [r.to_payload() for r in results], "alerts": alerts}


def loop_forever(store: Store, config_path: Path, interval: int, output_dir: Path, public_dir: Path) -> None:
    while True:
        started = utcnow()
        run_once(store, config_path, output_dir, public_dir)
        elapsed = (utcnow() - started).total_seconds()
        sleep_for = max(0, interval - elapsed)
        time.sleep(sleep_for)


def run_report(store: Store, day: Optional[str] = None) -> Dict[str, Any]:
    if day:
        try:
            dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        except Exception:
            dt = utcnow()
    else:
        dt = utcnow()
    return generate_daily_report(store, DEFAULT_REPORT_DIR, now=dt)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI service official status monitor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to services config JSON")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="sqlite db path")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="output dir for last_run/events")
    parser.add_argument("--public-dir", default=str(DEFAULT_PUBLIC_DIR), help="GitHub Pages-like output dir")

    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run one round")
    run.set_defaults(command="run")

    loop = subparsers.add_parser("loop", help="run repeatedly")
    loop.add_argument("--interval", type=int, default=60, help="seconds between runs")
    loop.set_defaults(command="loop")

    report = subparsers.add_parser("report", help="generate report for a day")
    report.add_argument("--day", required=False, help="YYYY-MM-DD")
    report.set_defaults(command="report")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = Store(Path(args.db))
    try:
        config_path = Path(args.config)
        output_dir = Path(args.output_dir)
        public_dir = Path(args.public_dir)

        if args.command == "run":
            result = run_once(store, config_path, output_dir, public_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "loop":
            loop_forever(store, config_path, args.interval, output_dir, public_dir)
        elif args.command == "report":
            result = run_report(store, day=getattr(args, "day", None))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            raise ValueError(f"Unsupported command: {args.command}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
