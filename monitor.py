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
        "none",
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
            return http_get(url, timeout=timeout, accept_json=accept_json, allow_unverified=False)
        except Exception as exc:
            last_error = exc
            if "amazonaws.com" in url:
                try:
                    return http_get(url, timeout=timeout, accept_json=accept_json, allow_unverified=True)
                except Exception as fallback_exc:
                    last_error = fallback_exc
            if attempt >= retries:
                break
            time.sleep(min(8, 1 << (attempt - 1)) + random.uniform(0, 0.35))
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


def _parse_grok_feed(feed_raw: Optional[bytes]) -> tuple[List[Dict[str, Any]], Optional[datetime], bool]:
    if not feed_raw:
        return [], None, False

    feed_text = decode_text(feed_raw)
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError:
        return [], None, False

    channel_updated = _parse_rfc_datetime((root.findtext(".//lastBuildDate") or "").strip())
    incidents = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = html.unescape((item.findtext("description") or "").strip())
        link = (item.findtext("link") or "").strip() or None
        guid = (item.findtext("guid") or "").strip() or None
        published = _parse_rfc_datetime((item.findtext("pubDate") or "").strip())
        text = f"{title} {desc}".lower()
        categories = {
            (category.text or "").strip().lower()
            for category in item.findall("category")
            if category.text
        }

        is_resolved = (
            "resolved" in categories
            or "available" in categories
            or "status: resolved" in text
            or "severity: available" in text
        )
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

    return incidents[:30], channel_updated, True


def parse_grok_service(
    page_raw: Optional[bytes],
    feed_raw: Optional[bytes],
    config: ServiceConfig,
    sample_time: datetime,
) -> CheckResult:
    incidents, feed_updated, feed_ok = _parse_grok_feed(feed_raw)
    page_text = decode_text(page_raw or b"")

    if incidents:
        overall_level = "critical" if any(item.get("status") == "critical" for item in incidents) else "warn"
    elif feed_ok:
        overall_level = "ok"
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
        confidence=0.9 if incidents else (0.85 if feed_ok else 0.45),
        updated_at=updated.isoformat().replace("+00:00", "Z") if isinstance(updated, datetime) else utciso(sample_time),
        source_url=config.source_url,
        level=overall_level,
        latest_incident_link=latest.get("link") if isinstance(latest, dict) else None,
    )


def _epoch_to_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except Exception:
        return None


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
        logs = _coerce_list(event.get("event_log"))
        latest_log = max(
            logs,
            key=lambda item: int(item.get("timestamp") or 0),
            default={},
        )
        updated_at_dt = _epoch_to_datetime(latest_log.get("timestamp")) or _epoch_to_datetime(event.get("date"))

        severity = "warn"
        # AWS public currentevents exposes numeric current/max impact values;
        # observed status 3 is the highest active impact level in the payload.
        if status_text == "3":
            severity = "critical"
        if any(token in summary.lower() for token in ["outage", "disruption", "unavailable"]):
            severity = "critical"
        if status_text and status_text not in {"0", "1", "2", "3", "4", "5"} and "critical" in status_text:
            severity = "critical"

        active_events.append(
            {
                "id": event.get("arn"),
                "name": f"{service_name} / {region}" if region else service_name,
                "status": event_status or "active",
                "summary": summary,
                "updated_at": updated_at_dt.isoformat().replace("+00:00", "Z") if updated_at_dt else None,
                "link": None,
                "region": region,
                "severity": severity,
                "latest_message": latest_log.get("message"),
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
        # Source failures are monitor health problems, not provider incidents.
        # Keep the existing alert latch untouched and do not count UNKNOWN as a
        # service degradation sample.
        consec_ok = 0

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
        unknown_count = level_counts.get("unknown", 0)
        measured_total = ok_count + warn_count + critical_count
        latest = entries[-1]
        recent = recent_incidents.get(service, [])
        summary["services"][service] = {
            "total_checks_24h": total,
            "measured_checks_24h": measured_total,
            "ok": ok_count,
            "warn": warn_count,
            "critical": critical_count,
            "unknown": unknown_count,
            "sla": round((ok_count / measured_total) * 100, 2) if measured_total else None,
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
        "| Service | SLA(OK%) | OK | WARN | CRITICAL | UNKNOWN | Latest |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for service, item in sorted(summary["services"].items(), key=lambda entry: entry[0]):
        sla_text = f"{item['sla']:.2f}" if item["sla"] is not None else "n/a"
        md_lines.append(
            f"| {service} | {sla_text} | {item['ok']} | {item['warn']} | "
            f"{item['critical']} | {item['unknown']} | {item['latest_status']} |"
        )
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


def _safe_level(level: Any) -> str:
    value = str(level or "unknown").strip().lower()
    return value if value in {"ok", "warn", "critical", "unknown"} else "unknown"


def _level_rank(level: str) -> int:
    return {"ok": 0, "unknown": 1, "warn": 2, "critical": 3}.get(_safe_level(level), 1)


def _format_seconds(seconds: Any) -> str:
    try:
        total = int(seconds)
    except Exception:
        return "-"
    if total < 60:
        return f"{total} 秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} 小时 {minutes % 60} 分钟"
    return f"{hours // 24} 天 {hours % 24} 小时"


def _display_time(value: Any) -> str:
    dt = parse_datetime(value)
    if not dt:
        return html.escape(str(value or "-"))
    beijing = dt.astimezone(timezone(timedelta(hours=8)))
    return (
        f"{html.escape(beijing.strftime('%Y-%m-%d %H:%M:%S'))} 北京时间"
        f" / {html.escape(dt.strftime('%Y-%m-%d %H:%M:%S'))} UTC"
    )


def _next_schedule_time(value: Any) -> str:
    dt = parse_datetime(value)
    if not dt:
        return "-"
    base = dt.replace(second=0, microsecond=0)
    next_minute = ((base.minute // 5) + 1) * 5
    if next_minute >= 60:
        next_dt = (base.replace(minute=0) + timedelta(hours=1))
    else:
        next_dt = base.replace(minute=next_minute)
    return _display_time(next_dt.isoformat().replace("+00:00", "Z"))


def _level_label(level: str) -> str:
    return {
        "ok": "正常",
        "warn": "预警",
        "critical": "严重",
        "unknown": "未知",
    }.get(_safe_level(level), "未知")


def _html_link(url: Optional[str], label: str) -> str:
    if not url:
        return ""
    safe_url = html.escape(str(url), quote=True)
    return f"<a href=\"{safe_url}\" rel=\"noreferrer\" target=\"_blank\">{html.escape(label)}</a>"


def _incident_name(incident: Dict[str, Any]) -> str:
    return str(
        incident.get("name")
        or incident.get("summary")
        or incident.get("message")
        or incident.get("id")
        or "事件"
    )


def render_public_page(last_run_path: Path, public_dir: Path) -> None:
    if not last_run_path.exists():
        return
    with open(last_run_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    services = sorted(
        data.get("services", []),
        key=lambda item: (-_level_rank(str(item.get("level"))), str(item.get("service", ""))),
    )
    level_counts = Counter(_safe_level(item.get("level")) for item in services)
    worst_level = services[0].get("level", "unknown") if services else "unknown"
    worst_level = _safe_level(worst_level)
    generated_at_raw = str(data.get("generated_at", ""))
    generated_at = _display_time(generated_at_raw)
    next_schedule_at = _next_schedule_time(generated_at_raw)
    repo_url = "https://github.com/study8677/aistatues"
    actions_url = "https://github.com/study8677/aistatues/actions/workflows/monitor.yml"
    title_by_level = {
        "ok": "当前监控服务均为正常",
        "warn": "部分监控服务出现降级或预警",
        "critical": "至少一个监控服务处于严重异常",
        "unknown": "监控源数据不完整",
    }

    cards = []
    incident_rows = []
    for item in services:
        level = _safe_level(item.get("level"))
        service = html.escape(str(item.get("service", "")))
        score = int(item.get("raw_score", 0) or 0)
        status = html.escape(str(item.get("overall_status", "")))
        source_url = str(item.get("source_url") or "")
        incident_link = str(item.get("latest_incident_link") or "")
        source_error = str(item.get("error") or "")
        incidents = item.get("active_incidents") if isinstance(item.get("active_incidents"), list) else []
        components = item.get("components") if isinstance(item.get("components"), list) else []
        first_incident = incidents[0] if incidents and isinstance(incidents[0], dict) else {}
        if first_incident:
            incident_label = html.escape(_incident_name(first_incident))
        elif source_error:
            incident_label = f"官方源错误：{html.escape(source_error)}"
        else:
            incident_label = "暂无进行中事件"
        active_count = len(incidents)
        duration = _format_seconds(item.get("anomaly_seconds"))
        confidence = f"{float(item.get('confidence', 0) or 0):.2f}"
        updated_at = _display_time(item.get("updated_at"))
        sampled_at = _display_time(item.get("time"))

        impacted_components = []
        for component in components:
            if not isinstance(component, dict):
                continue
            component_status = str(component.get("status", "unknown"))
            if level_from_indicator(component_status) != "ok" or level in {"warn", "critical"}:
                impacted_components.append(
                    f"{html.escape(str(component.get('name', 'Component')))} "
                    f"<span>{html.escape(component_status)}</span>"
                )
        component_text = ", ".join(impacted_components[:4]) if impacted_components else "官方源未报告受影响组件"
        if len(impacted_components) > 4:
            component_text += f"，另外 {len(impacted_components) - 4} 项"

        links = " ".join(
            part
            for part in [
                _html_link(source_url, "官方源"),
                _html_link(incident_link, "事件链接"),
            ]
            if part
        )
        badge_text = f"{_level_label(level)} {level.upper()}"

        cards.append(
            f"""
            <article class="service-card {level}">
              <div class="service-topline">
                <h2>{service}</h2>
                <span class="badge {level}">{badge_text}</span>
              </div>
              <div class="score-row">
                <strong>{score}</strong>
                <span>分数</span>
                <strong>{active_count}</strong>
                <span>进行中</span>
                <strong>{duration}</strong>
                <span>持续</span>
              </div>
              <dl>
                <div><dt>状态</dt><dd>{status}</dd></div>
                <div><dt>事件</dt><dd>{incident_label}</dd></div>
                <div><dt>组件</dt><dd>{component_text}</dd></div>
                <div><dt>源更新时间</dt><dd>{updated_at}</dd></div>
                <div><dt>采样时间</dt><dd>{sampled_at}</dd></div>
                <div><dt>置信度</dt><dd>{confidence}</dd></div>
              </dl>
              <div class="links">{links}</div>
            </article>
            """
        )
        for incident in incidents[:5]:
            if not isinstance(incident, dict):
                continue
            incident_rows.append(
                f"""
                <tr>
                  <td>{service}</td>
                  <td><span class="badge {level}">{badge_text}</span></td>
                  <td>{html.escape(_incident_name(incident))}</td>
                  <td>{html.escape(str(incident.get('status') or incident.get('severity') or 'active'))}</td>
                  <td>{_html_link(str(incident.get('link') or incident.get('url') or ''), '打开')}</td>
                </tr>
                """
            )

    html_body = f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset='utf-8' />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>AI 服务官方状态监控</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f6f7f9;
        --panel: #ffffff;
        --text: #18212f;
        --muted: #657083;
        --line: #d9dee7;
        --ok: #137333;
        --warn: #b06000;
        --critical: #b3261e;
        --unknown: #5f6368;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        background: var(--bg);
        color: var(--text);
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      header {{
        background: #ffffff;
        border-bottom: 1px solid var(--line);
      }}
      .wrap {{
        width: min(1180px, calc(100% - 32px));
        margin: 0 auto;
      }}
      .hero {{
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 24px;
        align-items: end;
        padding: 28px 0 22px;
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 28px;
        line-height: 1.15;
        letter-spacing: 0;
      }}
      .subtitle, .meta, dd, td {{
        color: var(--muted);
      }}
      .subtitle, .meta {{
        margin: 0;
        font-size: 14px;
      }}
      .summary {{
        display: flex;
        gap: 8px;
        justify-content: flex-end;
        flex-wrap: wrap;
      }}
      .hero-side {{
        display: grid;
        justify-items: end;
        gap: 10px;
      }}
      .header-actions {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        justify-content: flex-end;
      }}
      .header-actions a {{
        color: #0b57d0;
        font-size: 14px;
        font-weight: 700;
      }}
      .badge {{
        display: inline-flex;
        align-items: center;
        min-height: 24px;
        border-radius: 999px;
        padding: 3px 9px;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: 0;
      }}
      .badge.ok {{ color: var(--ok); background: #e7f4ea; }}
      .badge.warn {{ color: var(--warn); background: #fff3dc; }}
      .badge.critical {{ color: var(--critical); background: #fce8e6; }}
      .badge.unknown {{ color: var(--unknown); background: #eceff3; }}
      main {{
        padding: 24px 0 42px;
      }}
      .status-banner {{
        border: 1px solid var(--line);
        border-left-width: 6px;
        background: var(--panel);
        padding: 16px 18px;
        margin-bottom: 18px;
      }}
      .status-banner.ok {{ border-left-color: var(--ok); }}
      .status-banner.warn {{ border-left-color: var(--warn); }}
      .status-banner.critical {{ border-left-color: var(--critical); }}
      .status-banner.unknown {{ border-left-color: var(--unknown); }}
      .status-banner strong {{
        display: block;
        font-size: 18px;
        margin-bottom: 4px;
      }}
      .update-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 10px;
        margin: 0 0 18px;
      }}
      .update-item {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 12px 14px;
      }}
      .update-item strong {{
        display: block;
        font-size: 13px;
        margin-bottom: 4px;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
        gap: 14px;
      }}
      .service-card {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-top: 5px solid var(--unknown);
        border-radius: 8px;
        padding: 16px;
      }}
      .service-card.ok {{ border-top-color: var(--ok); }}
      .service-card.warn {{ border-top-color: var(--warn); }}
      .service-card.critical {{ border-top-color: var(--critical); }}
      .service-topline {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: center;
      }}
      h2 {{
        margin: 0;
        font-size: 18px;
        line-height: 1.25;
        letter-spacing: 0;
      }}
      .score-row {{
        display: grid;
        grid-template-columns: auto 1fr auto 1fr auto 1fr;
        gap: 4px 8px;
        align-items: baseline;
        margin: 14px 0;
        border-top: 1px solid var(--line);
        border-bottom: 1px solid var(--line);
        padding: 12px 0;
      }}
      .score-row strong {{
        font-size: 20px;
      }}
      .score-row span {{
        color: var(--muted);
        font-size: 12px;
      }}
      dl {{
        margin: 0;
        display: grid;
        gap: 8px;
      }}
      dl div {{
        display: grid;
        grid-template-columns: 96px 1fr;
        gap: 10px;
      }}
      dt {{
        color: #3c4656;
        font-size: 12px;
        font-weight: 800;
      }}
      dd {{
        margin: 0;
        overflow-wrap: anywhere;
      }}
      .links {{
        min-height: 24px;
        margin-top: 14px;
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }}
      a {{
        color: #0b57d0;
        text-decoration-thickness: 1px;
        text-underline-offset: 3px;
      }}
      section {{
        margin-top: 24px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        background: var(--panel);
        border: 1px solid var(--line);
      }}
      th, td {{
        text-align: left;
        padding: 10px 12px;
        border-bottom: 1px solid var(--line);
        vertical-align: top;
        font-size: 14px;
      }}
      th {{
        color: #3c4656;
        font-size: 12px;
        text-transform: uppercase;
      }}
      @media (max-width: 760px) {{
        .hero {{
          grid-template-columns: 1fr;
          align-items: start;
        }}
        .summary {{
          justify-content: flex-start;
        }}
        .hero-side {{
          justify-items: start;
        }}
        .header-actions {{
          justify-content: flex-start;
        }}
        table {{
          display: block;
          overflow-x: auto;
        }}
      }}
    </style>
  </head>
  <body>
    <header>
      <div class="wrap hero">
        <div>
          <h1>AI 服务官方状态监控</h1>
          <p class="subtitle">OpenAI、Claude、Gemini、Grok、AWS 官方状态源聚合看板。</p>
          <p class="meta">上次页面更新时间：{generated_at}</p>
        </div>
        <div class="hero-side">
          <div class="summary">
            <span class="badge critical">严重 {level_counts.get('critical', 0)}</span>
            <span class="badge warn">预警 {level_counts.get('warn', 0)}</span>
            <span class="badge ok">正常 {level_counts.get('ok', 0)}</span>
            <span class="badge unknown">未知 {level_counts.get('unknown', 0)}</span>
          </div>
          <nav class="header-actions" aria-label="项目链接">
            {_html_link(repo_url, "GitHub 仓库")}
            {_html_link(actions_url, "Actions 运行记录")}
          </nav>
        </div>
      </div>
    </header>
    <main class="wrap">
      <div class="status-banner {worst_level}">
        <strong>{html.escape(title_by_level.get(worst_level, title_by_level['unknown']))}</strong>
        <span class="meta">数据仅来自官方状态源，由 GitHub Actions 定时采样并发布到 GitHub Pages。</span>
      </div>
      <div class="update-grid">
        <div class="update-item">
          <strong>更新频率</strong>
          <span class="meta">每 5 分钟触发一次。</span>
        </div>
        <div class="update-item">
          <strong>触发时间</strong>
          <span class="meta">北京时间每小时 00、05、10、15、20、25、30、35、40、45、50、55 分。</span>
        </div>
        <div class="update-item">
          <strong>下一次预计触发</strong>
          <span class="meta">{next_schedule_at}</span>
        </div>
        <div class="update-item">
          <strong>实际生效</strong>
          <span class="meta">通常在触发后几十秒到数分钟内完成，取决于 GitHub Actions 排队和 Pages 缓存。</span>
        </div>
      </div>
      <div class="grid">
        {''.join(cards) if cards else '<p>暂无数据。</p>'}
      </div>
      <section>
        <h2>进行中事件</h2>
        <table>
          <thead>
            <tr><th>服务</th><th>等级</th><th>事件</th><th>状态</th><th>链接</th></tr>
          </thead>
          <tbody>
            {''.join(incident_rows) if incident_rows else '<tr><td colspan="5">官方源未报告进行中事件。</td></tr>'}
          </tbody>
        </table>
      </section>
    </main>
  </body>
</html>"""

    public_dir.mkdir(parents=True, exist_ok=True)
    with open(public_dir / "index.html", "w", encoding="utf-8") as fh:
        fh.write(html_body)
    with open(public_dir / "last_run.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


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
                components=[{"name": "Official source", "status": "unreachable"}],
                active_incidents=[],
                raw_score=score_from_level("unknown"),
                confidence=0.4,
                updated_at=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                source_url=config.source_url,
                level="unknown",
                error=f"{exc.code} {exc.reason}",
            )
        except Exception as exc:
            result = CheckResult(
                service=config.name,
                time=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                overall_status="error",
                components=[{"name": "Official source", "status": "unreachable"}],
                active_incidents=[],
                raw_score=score_from_level("unknown"),
                confidence=0.3,
                updated_at=utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                source_url=config.source_url,
                level="unknown",
                error=str(exc),
            )

        if result.level == "unknown":
            result.raw_score = score_from_level("unknown")
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
