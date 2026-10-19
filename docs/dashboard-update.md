# Dashboard diagnostics and presentation update

Change title: **Improve vibration diagnostics, recorded capture review, and demo dashboard**

The diagnostics page now combines waveform and spectrum views with acquisition details, a recorded-capture selector, baseline comparisons, a ten-peak table, and click-to-inspect frequency/amplitude/order readings. Manual RPM supports a display-only order axis when capture speed is missing.

Channel cards wrap into readable widths. Updated typography, navigation, controls, chart containers, acquisition panels, and mobile spacing give the dashboard a consistent presentation.

The public viewer demo has been regenerated from the application with synthetic data and supports the same diagnostic exploration. Configuration, deletion, and service actions remain disabled.

Historical reads use exact stored timestamps and channel identities. Missing legacy metadata stays unknown. Acquisition compatibility still gates baseline comparisons. Spectrum amplitudes remain device units; the order axis does not provide variable-speed order tracking. MQTT ingestion, database schemas, recording writes, and protocol services are unchanged.

Validation: 39 Python tests completed (2 Linux-only skips); four demo source/data checks passed; Ruff correctness checks passed; Chrome browser checks passed for all 14 routes, waveform display, capture selection, manual RPM, peak inspection, disabled actions, CSV export, and mobile overflow. Dashboard screenshots were refreshed.

Publishing: pushes to main trigger the existing Dashboard checks and Public dashboard demo workflows. The demo deploys to GitHub Pages after its checks pass; the container package publishes after its checks pass.

Follow-up: the device accordion uses one native expand/collapse arrow. Live order plots use dashboard RPM automatically; missing or invalid RPM prompts manual entry. A manual override is also available to correct an inaccurate RPM reading. Recorded captures retain their own RPM context.

Project name: **VDS-to-Dashboard**. Subtitle: Vibration monitoring and diagnostics for Wilcoxon VDS130. The existing repository URL, container package, and persistent-volume names remain stable for existing installations.
