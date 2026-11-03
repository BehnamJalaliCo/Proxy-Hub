# ruff: noqa: E501
"""Webhook bodies captured from the REAL pinned Alertmanager (prom/alertmanager v0.34.2, digest
sha256:39d8bb39...) on 2026-11-03, byte for byte as received (test data, decision D10).

How they were captured: the image ran with the real deploy/monitoring/alertmanager/entrypoint.sh
(so the rendered "owner" receiver posts to http://api:8000/internal/alerts/alertmanager) on an
internal Docker network where a plain HTTP receiver answered as "api:8000" and saved each POST body.
Alerts were added with ``amtool alert add`` using the same labels and Persian annotations as the
rules in deploy/monitoring/prometheus/rules/panel.yml; one alert got an end time so a "resolved"
notification followed, and one group had one alert resolve while the other kept firing (MIXED).
Headers of every request: ``POST /internal/alerts/alertmanager``, ``User-Agent: Alertmanager/0.34.2``,
``Content-Type: application/json``.
"""

from __future__ import annotations

import json

from proxyhub.telegram.alerts import AlertmanagerWebhook

FIRING_WORKER_STALE = r"""{"receiver":"owner","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"WorkerHeartbeatStale","instance":"api:8000","job":"proxyhub-api","severity":"critical"},"annotations":{"description":"آخرین ضربان worker ۳ دقیقه و ۲۰ ثانیه پیش بوده است (حد مجاز ۹۰ ثانیه). چرخه‌های کنترل اجرا نمیشوند.","summary":"worker پنل از کار افتاده است"},"startsAt":"2026-11-03T20:24:26Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"http://prometheus:9090/graph?g0.expr=proxyhub_worker_heartbeat_age_seconds+%3E+90\u0026g0.tab=1","fingerprint":"e542b2fada2d74e3"}],"notification_reason":"first notification","groupLabels":{"alertname":"WorkerHeartbeatStale"},"commonLabels":{"alertname":"WorkerHeartbeatStale","instance":"api:8000","job":"proxyhub-api","severity":"critical"},"commonAnnotations":{"description":"آخرین ضربان worker ۳ دقیقه و ۲۰ ثانیه پیش بوده است (حد مجاز ۹۰ ثانیه). چرخه‌های کنترل اجرا نمیشوند.","summary":"worker پنل از کار افتاده است"},"routeLabels":{},"externalURL":"http://d0e855448b31:9093","version":"4","groupKey":"{}:{alertname=\"WorkerHeartbeatStale\"}","truncatedAlerts":0}"""

FIRING_DISK_TWO = r"""{"receiver":"owner","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"DiskAlmostFull","device":"/dev/sda1","fstype":"ext4","instance":"node-exporter:9100","job":"node","mountpoint":"/","severity":"warning"},"annotations":{"description":"۹۰٪ از دیسک / پر است (حد هشدار ۸۵٪).","summary":"دیسک سرور پنل نزدیک پر شدن است"},"startsAt":"2026-11-03T20:23:17.901536422Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"","fingerprint":"4ae4b489d4cbf887"},{"status":"firing","labels":{"alertname":"DiskAlmostFull","device":"/dev/sdb1","fstype":"ext4","instance":"node-exporter:9100","job":"node","mountpoint":"/data","severity":"warning"},"annotations":{"description":"۸۷٪ از دیسک /data پر است (حد هشدار ۸۵٪).","summary":"دیسک سرور پنل نزدیک پر شدن است"},"startsAt":"2026-11-03T20:23:18.031246708Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"","fingerprint":"a468f9f753612064"}],"notification_reason":"first notification","groupLabels":{"alertname":"DiskAlmostFull"},"commonLabels":{"alertname":"DiskAlmostFull","fstype":"ext4","instance":"node-exporter:9100","job":"node","severity":"warning"},"commonAnnotations":{"summary":"دیسک سرور پنل نزدیک پر شدن است"},"routeLabels":{},"externalURL":"http://d0e855448b31:9093","version":"4","groupKey":"{}:{alertname=\"DiskAlmostFull\"}","truncatedAlerts":0}"""

RESOLVED_WORKER_STALE = r"""{"receiver":"owner","status":"resolved","alerts":[{"status":"resolved","labels":{"alertname":"WorkerHeartbeatStale","instance":"api:8000","job":"proxyhub-api","severity":"critical"},"annotations":{"description":"آخرین ضربان worker ۳ دقیقه و ۲۰ ثانیه پیش بوده است (حد مجاز ۹۰ ثانیه). چرخه‌های کنترل اجرا نمیشوند.","summary":"worker پنل از کار افتاده است"},"startsAt":"2026-11-03T20:24:26Z","endsAt":"2026-11-03T20:24:26Z","generatorURL":"http://prometheus:9090/graph?g0.expr=proxyhub_worker_heartbeat_age_seconds+%3E+90\u0026g0.tab=1","fingerprint":"e542b2fada2d74e3"}],"notification_reason":"all alerts resolved","groupLabels":{"alertname":"WorkerHeartbeatStale"},"commonLabels":{"alertname":"WorkerHeartbeatStale","instance":"api:8000","job":"proxyhub-api","severity":"critical"},"commonAnnotations":{"description":"آخرین ضربان worker ۳ دقیقه و ۲۰ ثانیه پیش بوده است (حد مجاز ۹۰ ثانیه). چرخه‌های کنترل اجرا نمیشوند.","summary":"worker پنل از کار افتاده است"},"routeLabels":{},"externalURL":"http://d0e855448b31:9093","version":"4","groupKey":"{}:{alertname=\"WorkerHeartbeatStale\"}","truncatedAlerts":0}"""

RESOLVED_DISK_TWO = r"""{"receiver":"owner","status":"resolved","alerts":[{"status":"resolved","labels":{"alertname":"DiskAlmostFull","device":"/dev/sda1","fstype":"ext4","instance":"node-exporter:9100","job":"node","mountpoint":"/","severity":"warning"},"annotations":{"description":"۹۰٪ از دیسک / پر است (حد هشدار ۸۵٪).","summary":"دیسک سرور پنل نزدیک پر شدن است"},"startsAt":"2026-11-03T20:23:17.901536422Z","endsAt":"2026-11-03T20:28:17.901536422Z","generatorURL":"","fingerprint":"4ae4b489d4cbf887"},{"status":"resolved","labels":{"alertname":"DiskAlmostFull","device":"/dev/sdb1","fstype":"ext4","instance":"node-exporter:9100","job":"node","mountpoint":"/data","severity":"warning"},"annotations":{"description":"۸۷٪ از دیسک /data پر است (حد هشدار ۸۵٪).","summary":"دیسک سرور پنل نزدیک پر شدن است"},"startsAt":"2026-11-03T20:23:18.031246708Z","endsAt":"2026-11-03T20:28:18.031246708Z","generatorURL":"","fingerprint":"a468f9f753612064"}],"notification_reason":"all alerts resolved","groupLabels":{"alertname":"DiskAlmostFull"},"commonLabels":{"alertname":"DiskAlmostFull","fstype":"ext4","instance":"node-exporter:9100","job":"node","severity":"warning"},"commonAnnotations":{"summary":"دیسک سرور پنل نزدیک پر شدن است"},"routeLabels":{},"externalURL":"http://d0e855448b31:9093","version":"4","groupKey":"{}:{alertname=\"DiskAlmostFull\"}","truncatedAlerts":0}"""

FIRING_REDIS_TWO = r"""{"receiver":"owner","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"PanelRedisDown","instance":"api:8000","job":"proxyhub-api","severity":"critical"},"annotations":{"description":"API پنل بیش از ۱ دقیقه به Redis وصل نشده است.","summary":"Redis پنل پاسخ نمیدهد"},"startsAt":"2026-11-03T20:37:59Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"","fingerprint":"0e3d2a7152e3acf5"},{"status":"firing","labels":{"alertname":"PanelRedisDown","instance":"other:8000","job":"proxyhub-api","severity":"critical"},"annotations":{"description":"API پنل بیش از ۱ دقیقه به Redis وصل نشده است.","summary":"Redis پنل پاسخ نمیدهد"},"startsAt":"2026-11-03T20:36:59.923279636Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"","fingerprint":"b796ab622eac62ef"}],"notification_reason":"first notification","groupLabels":{"alertname":"PanelRedisDown"},"commonLabels":{"alertname":"PanelRedisDown","job":"proxyhub-api","severity":"critical"},"commonAnnotations":{"description":"API پنل بیش از ۱ دقیقه به Redis وصل نشده است.","summary":"Redis پنل پاسخ نمیدهد"},"routeLabels":{},"externalURL":"http://d0e855448b31:9093","version":"4","groupKey":"{}:{alertname=\"PanelRedisDown\"}","truncatedAlerts":0}"""

MIXED_REDIS = r"""{"receiver":"owner","status":"firing","alerts":[{"status":"resolved","labels":{"alertname":"PanelRedisDown","instance":"api:8000","job":"proxyhub-api","severity":"critical"},"annotations":{"description":"API پنل بیش از ۱ دقیقه به Redis وصل نشده است.","summary":"Redis پنل پاسخ نمیدهد"},"startsAt":"2026-11-03T20:37:59Z","endsAt":"2026-11-03T20:37:59Z","generatorURL":"","fingerprint":"0e3d2a7152e3acf5"},{"status":"firing","labels":{"alertname":"PanelRedisDown","instance":"other:8000","job":"proxyhub-api","severity":"critical"},"annotations":{"description":"API پنل بیش از ۱ دقیقه به Redis وصل نشده است.","summary":"Redis پنل پاسخ نمیدهد"},"startsAt":"2026-11-03T20:36:59.923279636Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"","fingerprint":"b796ab622eac62ef"}],"notification_reason":"some alerts resolved","groupLabels":{"alertname":"PanelRedisDown"},"commonLabels":{"alertname":"PanelRedisDown","job":"proxyhub-api","severity":"critical"},"commonAnnotations":{"description":"API پنل بیش از ۱ دقیقه به Redis وصل نشده است.","summary":"Redis پنل پاسخ نمیدهد"},"routeLabels":{},"externalURL":"http://d0e855448b31:9093","version":"4","groupKey":"{}:{alertname=\"PanelRedisDown\"}","truncatedAlerts":0}"""

ALL = {
    "FIRING_WORKER_STALE": FIRING_WORKER_STALE,
    "FIRING_DISK_TWO": FIRING_DISK_TWO,
    "RESOLVED_WORKER_STALE": RESOLVED_WORKER_STALE,
    "RESOLVED_DISK_TWO": RESOLVED_DISK_TWO,
    "FIRING_REDIS_TWO": FIRING_REDIS_TWO,
    "MIXED_REDIS": MIXED_REDIS,
}


def test_captured_bodies_parse_with_our_model() -> None:
    for name, raw in ALL.items():
        data = json.loads(raw)
        hook = AlertmanagerWebhook.model_validate(data)
        assert hook.version == "4" and hook.receiver == "owner", name
        assert hook.alerts and all(a.fingerprint for a in hook.alerts), name
    mixed = AlertmanagerWebhook.model_validate_json(MIXED_REDIS)
    assert mixed.status == "firing"
    assert sorted(a.status for a in mixed.alerts) == ["firing", "resolved"]
