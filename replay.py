"""Replay documented or captured VDS JSON into a new isolated SQLite historian."""

import argparse
import json
import os
from pathlib import Path
import time
from datetime import datetime, timedelta
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON object or array of VDS messages")
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="New directory; existing databases are refused",
    )
    parser.add_argument(
        "--rebase-time",
        action="store_true",
        help="Shift captures to now, preserving relative timing",
    )
    parser.add_argument(
        "--record-raw", action="store_true", help="Include waveform and spectrum arrays"
    )
    args = parser.parse_args()
    destination = args.data_dir.resolve()
    if (destination / "vibration_trends.db").exists():
        parser.error("Use a new data directory to avoid changing an existing historian")
    messages = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, list) or not messages:
        parser.error("Input must contain one or more messages")
    from vds_dashboard.vds import normalize_packet

    for message in messages:
        normalize_packet(message)
    if args.rebase_time:
        shift = int(time.time() * 1000) - max(
            message["timestamp"] for message in messages
        )
        for message in messages:
            message["timestamp"] += shift
    os.environ["VDS_DATA_DIR"] = str(destination)
    os.environ["VDS_CONFIG_DIR"] = str(destination / "config")
    import app

    for message in messages:
        if args.record_raw:
            app.active_jobs[message["gateway_id"]] = {
                "job_id": "isolated-replay",
                "end_time": datetime.now() + timedelta(hours=1),
            }
        app.on_message(
            None, None, SimpleNamespace(payload=json.dumps(message).encode())
        )
    print(
        f"Replayed {len(messages)} messages into {app.DB_FILE}. No MQTT or PLC services were started."
    )


if __name__ == "__main__":
    main()
