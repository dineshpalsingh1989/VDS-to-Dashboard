"""Small, dependency-free runtime helpers shared by the dashboard and tests."""

import json
import math
import os
import tempfile
import io
import pickle
import copy
import logging


class ConfigUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError(
            "Executable objects are not allowed in configuration"
        )


def decode_config(blob):
    try:
        result = json.loads(blob)
    except (ValueError, UnicodeDecodeError):
        result = ConfigUnpickler(io.BytesIO(blob)).load()
    if not isinstance(result, dict):
        raise ValueError("Configuration must be an object")
    # Verify the legacy value can be safely migrated to JSON.
    json.dumps(result, allow_nan=False)
    return result


def atomic_json_write(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json_config(path, defaults):
    result = copy.deepcopy(defaults)
    if not os.path.exists(path):
        atomic_json_write(path, result)
        return result
    try:
        with open(path, encoding="utf-8") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict):
            raise ValueError("Configuration must be an object")
        result.update(loaded)
    except (OSError, ValueError):
        logging.exception("Cannot read configuration %s; using defaults", path)
    return result


def normalize_event(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Sensor payload must be an object")
    gateway = payload.get("gateway_id")
    if not isinstance(gateway, str) or not gateway.strip() or len(gateway) > 128:
        raise ValueError("Invalid gateway_id")
    if not isinstance(payload.get("sensor_id"), str):
        raise ValueError("Invalid sensor_id")
    timestamp = payload.get("timestamp")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or not 0 < timestamp < 32503680000000
    ):
        raise ValueError("Invalid timestamp in milliseconds")
    for key in ("Time waveform", "Power spectrum"):
        if key in payload:
            values = payload[key]
            if not isinstance(values, list) or len(values) > 262144:
                raise ValueError("Invalid or oversized sensor array")
            if any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in values
            ):
                raise ValueError("Sensor samples must be finite numbers")
    for key in ("sampling_frequency", "bandwidth", "lines", "duration"):
        value = payload.get(key, 1)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("Invalid " + key)
    process = payload.get("process_data", {})
    if not isinstance(process, dict):
        raise ValueError("Invalid process_data")
    for key in ("Acceleration", "Velocity", "Displacement", "CrestFactor", "TruePeak", "StandardDeviation"):
        metric = process.get(key, {})
        if not isinstance(metric, dict):
            raise ValueError("Invalid metric " + key)
        for field in ('unit', 'filter', 'detection'):
            if field in metric and (not isinstance(metric[field], str) or len(metric[field]) > 128):
                raise ValueError('Invalid measurement metadata')
        value = metric.get("value", 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid metric value")
    rpm = process.get("RPM", 0)
    if (
        isinstance(rpm, bool)
        or not isinstance(rpm, (int, float))
        or not math.isfinite(rpm)
        or rpm < 0
    ):
        raise ValueError("Invalid RPM")
    return payload
