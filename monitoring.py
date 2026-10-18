"""Deterministic advisory agents. Findings never trigger machine commands."""

import time
from datetime import datetime

from vds import freshness, signature


def assess_health(devices, mqtt_status, disk_free_percent, now=None, baselines=None):
    epoch = (
        now.timestamp()
        if isinstance(now, datetime)
        else (time.time() if now is None else now)
    )
    findings = []
    if mqtt_status != "connected":
        findings.append(
            (
                "warning",
                "Connectivity agent",
                "MQTT is disconnected. Check the broker address, credentials, and network.",
            )
        )
    if disk_free_percent < 10:
        findings.append(
            (
                "danger",
                "Storage agent",
                f"Only {disk_free_percent:.1f}% disk space remains. Export and archive recordings.",
            )
        )
    for gid, device in sorted(devices.items()):
        channels = device.get("channels") or {"device": device}
        for ch, data in sorted(channels.items()):
            quality = freshness(data, epoch)
            label = f"{gid} CH{ch}"
            if quality["status"] != "fresh":
                findings.append(
                    (
                        "warning",
                        "Sensor agent",
                        f"{label}: {quality['status']}; expected interval {data.get('publish_interval', 60)}s, stale threshold {quality['limit']:.0f}s.",
                    )
                )
            if data.get("quality_issues"):
                findings.append(
                    (
                        "warning",
                        "Data-quality agent",
                        f"{label}: " + "; ".join(data["quality_issues"]),
                    )
                )
            baseline = (baselines or {}).get((gid, ch), {})
            for metric, reference in baseline.get("metrics", {}).items():
                current = data.get("metrics", {}).get(metric)
                before = baseline.get("metadata", {}).get(metric, {})
                after = data.get("metadata", {}).get(metric, {})
                if (
                    current is None
                    or not reference
                    or not quality["valid"]
                    or any(
                        not before.get(key) for key in ("unit", "filter", "detection")
                    )
                ):
                    continue
                if signature(before) != signature(after):
                    findings.append(
                        (
                            "info",
                            "Baseline agent",
                            f"{label} {metric}: settings changed; baseline comparison withheld.",
                        )
                    )
                    continue
                change = (current - reference) / abs(reference) * 100
                if abs(change) >= 50:
                    findings.append(
                        (
                            "warning",
                            "Baseline agent",
                            f"{label} {metric}: {reference:.4g} → {current:.4g} {after.get('unit', '')}, {change:+.1f}% versus saved baseline. Review operating conditions.",
                        )
                    )
    if not devices:
        findings.append(
            ("info", "Sensor agent", "Waiting for the first sensor message.")
        )
    if not findings:
        findings.append(
            (
                "success",
                "Health agent",
                f"MQTT is connected, {len(devices)} devices are fresh, and storage capacity is healthy.",
            )
        )
    return findings
