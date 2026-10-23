# Public viewer demo

The public preview is exported from the **actual rendered Dash application**, using
`app.py`, `vds_dashboard/workspace_views.py`, and `assets/dashboard.css`. Its 14 pages retain
the real header, menu, device accordion, measurement/RPM cards, forms, and charts.
Only the synthetic-data notice and offline exploration behavior are demo-specific.
Dropdowns use native HTML selects in place of Dash's server-driven dropdowns.

All measurements, alarm events, asset identities, and host information are invented.
The UI displays a captured dataset, not live telemetry. There are no backend action
handlers. Settings are read-only; save/delete/acknowledge/backup/restart/service
controls are disabled. History CSV export uses sample values only.

Regenerate from the repository root with application dependencies, Node.js 24,
and Chrome installed:

```bash
node scripts/build-demo.mjs
node --test tests/demo-data.mjs
node scripts/demo-smoke.mjs
```

`DEMO_PYTHON` selects Python; `DEMO_BROWSER` selects Chrome. GitHub Actions renders
and checks the preview before publishing it, including changes to application
layouts and styles. Source hashes detect stale checked-in snapshots. The builder
uses a new temporary database/configuration directory, never starts device
services, and publishes only static files. No production data is exported.

`snapshots.json`, `vendor/dashboard.css`, and `vendor/components.css` are generated.
Do not edit generated layouts to change the application. Chart data is generated
by `scripts/demo-fixture.py`, including the real diagnostic/history plot functions.

Bundled third-party files preserve their notices: Bootstrap 5.3.6 (MIT),
Font Awesome Free 6.7.2 (see `vendor/fontawesome-LICENSE.txt`), and the Plotly.js
version included with the installed Python Plotly package (MIT notice in
`vendor/plotly.min.js`). Download origins are recorded in `vendor/sources.json`.

Diagnostic exploration includes waveform and spectrum plots, capture selection,
acquisition details, manual RPM, strongest local peaks, and click-to-inspect readings.
Twelve synthetic captures per channel illustrate comparison with a saved baseline.
These display controls work offline; they do not enable configuration or service
changes. Manual RPM affects only spectrum plotting; waterfall frequencies stay in Hz.

Live/latest sample order plots use the demo dashboard RPM automatically; recorded
captures retain capture RPM. Manual RPM remains editable when a correction is needed.
