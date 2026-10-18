# Working on this dashboard

Keep MQTT ingestion, SQLite persistence, and protocol gateways deterministic.
Advisory agents may report findings; they must not issue machine commands.

Before changing ingestion or recording, run `python -m unittest discover -s tests -v`.
Tests must use temporary databases and must not connect to a real broker, PLC, or sensor.
Never commit configuration credentials, runtime databases, or recordings.

Preserve existing recording data and plain legacy configuration when migrating formats.
Keep database writes outside `data_lock`; do not open competing write transactions.
Viewer access must never permit configuration changes, deletion, or service controls.
Protocol status must reflect the running service, rather than only the requested state.
Keep PyModbus pinned until an explicit API migration is implemented and tested.
