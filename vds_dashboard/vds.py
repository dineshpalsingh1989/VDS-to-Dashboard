"""Wilcoxon VDS message normalization and measurement-quality rules.

Schema reference: VDS130 setup instructions 97047 Rev.A, page 10.
Unknown measurement settings remain unknown, rather than receiving assumed units.
"""

import json
import math
import time
from datetime import datetime

import numpy as np

from .runtime import validate_payload

METRIC_KEYS = {
    "Acceleration": "Acceleration",
    "Velocity": "Velocity",
    "Displacement": "Displacement",
    "CrestFactor": "Crest Factor",
    "TruePeak": "True Peak",
    "StandardDeviation": "Std Deviation",
}


def signature(metadata):
    return json.dumps(
        {key: metadata.get(key, "") for key in ("unit", "filter", "detection")},
        sort_keys=True,
    )


def normalize_packet(payload, received_at=None):
    validate_payload(payload)
    channel = payload["sensor_id"].rsplit("_", 1)[-1]
    if channel not in ("1", "2", "3", "4"):
        raise ValueError("VDS channel must be 1 through 4")
    process = payload.get("process_data", {})
    metrics, metadata, issues = {}, {}, []
    for source, target in METRIC_KEYS.items():
        if source in process:
            metric = process[source]
            metrics[target] = float(metric["value"])
            metadata[target] = {
                key: metric.get(key, "") for key in ("unit", "filter", "detection")
            }
            if any(
                not metadata[target][key] for key in ("unit", "filter", "detection")
            ):
                issues.append(f"{target}: incomplete measurement settings")
    waveform = payload.get("Time waveform")
    spectrum = payload.get("Power spectrum")
    if not metrics and waveform is None and spectrum is None:
        raise ValueError("Message contains no measurements")
    fs = payload.get("sampling_frequency")
    duration = payload.get("duration")
    lines = payload.get("lines")
    bandwidth = payload.get("bandwidth")
    if waveform is not None:
        if not fs:
            issues.append("Waveform sampling frequency missing")
        if duration is not None and fs and len(waveform) != round(duration * fs):
            issues.append(
                "Waveform length does not match duration × sampling frequency"
            )
    if spectrum is not None:
        if not lines or not bandwidth:
            issues.append("Spectrum bandwidth or line count missing")
        elif len(spectrum) != lines + 1:
            issues.append("Spectrum length does not match lines + 1")
    return {
        "gateway_id": payload["gateway_id"],
        "channel": channel,
        "timestamp_ms": int(payload["timestamp"]),
        "received_at": time.time() if received_at is None else received_at,
        "metrics": metrics,
        "metadata": metadata,
        "rpm": process.get("RPM"),
        "sensor_type": process.get("SensorType", ""),
        "sensitivity": process.get("SensorSensitivity"),
        "sampling_freq": fs,
        "duration": duration,
        "bandwidth": bandwidth,
        "lines": lines,
        "window": payload.get("window", ""),
        "waveform_data": waveform,
        "spectrum_data": spectrum,
        "quality_issues": issues,
    }


def freshness(data, now=None):
    now = time.time() if now is None else now
    interval = float(data.get("publish_interval") or 60)
    limit = max(1, interval) * 3
    capture = data.get("timestamp_ms")
    if capture is None:
        try:
            capture = (
                datetime.strptime(
                    data.get("timestamp", data.get("last_seen", "")),
                    "%Y-%m-%d %H:%M:%S",
                ).timestamp()
                * 1000
            )
        except (ValueError, TypeError):
            return {"age": None, "valid": False, "status": "unknown", "limit": limit}
    age = now - capture / 1000
    receipt_age = now - data.get("received_at", capture / 1000)
    status = (
        "clock error"
        if age < -max(60, interval)
        else "stale"
        if max(age, receipt_age) > limit
        else "fresh"
    )
    return {
        "age": max(0, age),
        "valid": status == "fresh" and not data.get("quality_issues"),
        "status": status,
        "limit": limit,
    }


def spectrum_xy(snapshot, orders=False):
    values = np.asarray(snapshot.get("spectrum_data", []), dtype=float)
    bandwidth, lines = snapshot.get("bandwidth"), snapshot.get("lines")
    if not values.size or not bandwidth or not lines or values.size != lines + 1:
        raise ValueError(
            "A spectrum needs valid bandwidth, lines, and lines + 1 samples"
        )
    frequencies = np.arange(values.size) * bandwidth / lines
    if orders:
        rpm = snapshot.get("rpm")
        if rpm is None or rpm <= 0:
            raise ValueError("Order view requires a positive RPM measurement")
        frequencies = frequencies / (rpm / 60)
    return frequencies, values


def spectrum_signature(snapshot):
    return json.dumps(
        {
            key: snapshot.get(key)
            for key in (
                "bandwidth",
                "lines",
                "sampling_freq",
                "window",
                "sensor_type",
                "sensitivity",
            )
        },
        sort_keys=True,
    )


def protocol_value(device, channel, field, connected=True, now=None):
    data = (
        device if channel == "RPM" else device.get("channels", {}).get(str(channel), {})
    )
    quality = freshness(data, now)
    if field == "Data Age":
        return quality["age"] if quality["age"] is not None else -1.0
    if field == "Data Valid":
        epoch = time.time() if now is None else now
        metrics = data.get("metrics", {})
        details = data.get("metadata", {})
        metric_times = data.get("metric_times", {})
        consistent = bool(metrics) and all(
            all(
                details.get(metric, {}).get(key)
                for key in ("unit", "filter", "detection")
            )
            and metric in metric_times
            and -60 <= epoch - metric_times[metric] / 1000 <= quality["limit"]
            for metric in metrics
        )
        return float(quality["valid"] and consistent)
    if field == "Communication Status":
        return float(connected and quality["status"] == "fresh")
    if field == "RPM":
        return float(device.get("rpm") or 0)
    return float(data.get("metrics", {}).get(field, 0))


def validate_alarm(rule):
    warn, alert = float(rule["warn"]), float(rule["alert"])
    hysteresis = float(rule.get("hysteresis", 0))
    count = int(rule.get("persistence", 2))
    if (
        not all(math.isfinite(v) for v in (warn, alert, hysteresis))
        or not 0 <= hysteresis < warn < alert
        or not 1 <= count <= 100
    ):
        raise ValueError(
            "Require 0 ≤ hysteresis < warning < alert and persistence between 1 and 100"
        )
    if any(
        not rule.get("metadata", {}).get(key) for key in ("unit", "filter", "detection")
    ):
        raise ValueError(
            "Choose a current measurement with unit, filter, and detection before enabling alarms"
        )
    return dict(rule, warn=warn, alert=alert, hysteresis=hysteresis, persistence=count)


def advance_alarm(previous, value, metadata, rule):
    rule = validate_alarm(rule)
    previous = previous or {"severity": "normal", "candidate": "normal", "count": 0}
    if signature(metadata) != signature(rule["metadata"]):
        return {
            "severity": "unknown",
            "candidate": "unknown",
            "count": 0,
            "reason": "Measurement settings changed; review the alarm rule",
        }
    current = previous["severity"]
    if current == "alert" and value >= rule["alert"] - rule["hysteresis"]:
        target = "alert"
    elif value >= rule["alert"]:
        target = "alert"
    elif current in ("warning", "alert") and value >= rule["warn"] - rule["hysteresis"]:
        target = "warning"
    elif value >= rule["warn"]:
        target = "warning"
    else:
        target = "normal"
    count = previous.get("count", 0) + 1 if previous.get("candidate") == target else 1
    severity = target if target == current or count >= rule["persistence"] else current
    return {
        "severity": severity,
        "candidate": target,
        "count": min(count, rule["persistence"]),
        "reason": "",
    }
