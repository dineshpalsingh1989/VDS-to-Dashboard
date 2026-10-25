# Plan: teaching the IRIV Edge AI from real data

Status: agreed, not started (2026-10-25). Decision: training runs in Google Cloud, next to
the data; GitHub holds only the code. Nothing described here is built yet; today
nothing leaves the device and all data stays in SQLite on the IRIV.

## What already learns on the device

- Each channel's normal spectrum, envelope, waveform and readings, per speed.
- Fault patterns people confirm (recognised again on any channel as "seen before").
- Wrong and Repaired feedback, which teach what is normal for that machine.

The neural second opinion on the Hailo AI HAT+ does not retrain itself: the HAT can
only run a model, and building the HAT version needs Hailo's compiler on an x86 computer.

## What gets collected: machine cases

One case is one machine at one moment, with all its channels (usually 2 or 3):

- Machine: type, power, foundation (ISO class), running speed and where it came from.
- Each channel: part (motor, pump, fan, gearbox), drive or free end, horizontal,
  vertical or axial, bearing number, sensor type and sensitivity, plus the spectrum,
  waveform and overall readings from the same moment.
- The answer: Confirmed, Wrong (normal) or Repaired, and the AI's own finding.
- Occasional "running normally" cases, so the model does not learn only faults.

About 50 KB per case compressed. Sharing is opt-in per customer, serial numbers and
machine names are replaced by codes, and a page shows exactly what is sent. The
advisor stays offline; a separate small uploader sends the cases.

The model gets one input slot per channel (up to 4), each with that channel's signals
and position, so it learns cross-channel patterns such as axial against radial or
drive end against free end. Empty slots are allowed.

## Where training runs

| Stage | Where | Cost |
|---|---|---|
| Cases stored | Google Cloud Storage (BigQuery optional later, for reports only) | cents a month |
| Training and HAT build, automatic (chosen) | Google Cloud: a Compute Engine VM or Vertex AI job, weekly or monthly, switched off after each run | about $1–5 a month ($300 free credit for new accounts) |
| Trying it out by hand | Your PC, or Google Colab | free |

Customer cases never go to GitHub: the repository is public, so its job logs, job files
and releases are public too.

## Design decisions (2026-10-25)

**Federated learning.** Customer data stays on the Pi. Each Pi keeps its own cases,
trains the shared model a little overnight on them (about a minute on the processor),
and sends only the change in the model's numbers (about 140 KB) plus how many cases it
used. Once a week Google Cloud averages the changes from all Pis (weighted by case
count, with a limit on how much one Pi can move the model), checks the result on
synthetic fault cases and on each Pi's reported accuracy, builds the HAT files and
publishes the new shared model. Each Pi tests it in shadow mode before switching, and
uses its own locally trained version on the processor in between.

**Send once, only on change.** Nothing streams.

| Item | Sent |
|---|---|
| Channel setup (machine, type, power, foundation, part, end, direction, bearing, sensor, speed source) | once when complete, again only if changed (under 1 KB) |
| Learned normal fingerprint per speed | once when learning finishes, again after Relearn or Repaired (10–20 KB) |
| Labelled case (Confirmed, Wrong, Repaired), only if the customer allows examples | once per event, then marked sent (about 50 KB) |
| Model change | only on nights with new cases (about 140 KB) |
| Live readings, trends, every spectrum | never |

Items wait on the Pi while offline and go once when the connection returns.

**One owner account, a folder per customer.** One Cloud Storage bucket, owned by the
super user (the product owner):

```
customers/<customer>/<device>/   setup, normals, reports, model changes (+ cases if allowed)
models/current/                  the shared model every device downloads
models/history/                  earlier versions, for rollback
```

Each device has its own key: it can write only its own folder and read `models/`.
The owner sees every customer and device, last contact, case counts, training runs
and model versions, releases new models and can switch off a device. A device joins
with a one-time connection code that the owner creates per customer.

**Customer settings page: Settings > AI learning (IRIV only, administrators change it).**

- Switch "Share learning with the VDS AI network", off by default.
- What to share: "Learning only" (default: setup, normals, model changes) or "Learning
  and examples" (also labelled cases with spectra and waveforms, sent once).
- What is sent, with sizes, and a Preview download of exactly what would go.
- What is never sent: live readings, trends, passwords, site names; machine names and
  serial numbers are replaced by codes.
- What is received: new shared models with version and what improved, tested in shadow
  mode first; "Install automatically after testing" or "Ask me first"; Undo to the
  previous model.
- Status: connected organisation, customer and device code, last sent, model in use.
- Stop and delete: switches sharing off and asks the cloud to delete this device's folder.
- A short data agreement (what, why, how long, how to delete) shown and accepted on the
  page, recording who agreed and when.

## Steps to build (later)

1. Settings > AI learning page, connection code, the "sent" list, setup and normal
   fingerprints sent once, optional cases, Preview download, and the uploader.
2. Retrain the small model on the Pi's processor overnight from confirmed cases.
3. Google Cloud job: combine the model changes (federated), check, build the HAT files
   (hailo8 and hailo8l), publish to models/current with history; an owner view of
   customers, devices and model releases.
4. On the IRIV: download new models, run them in shadow mode beside the current one,
   switch only if they agree better with people's feedback, roll back automatically.

## One-time steps for the owner

1. Create a Hailo Developer Zone account and download the Dataflow Compiler.
2. Check that Hailo's licence allows running the compiler on Google Cloud machines.
3. Create a Google Cloud project with a private Storage bucket, and keep the compiler there.
4. Merge to `main` and push release tags.
5. Write the customer data agreement text.
6. Each customer enters their connection code and switches sharing on.

After that it runs by itself: the Pi learns, cases are collected, the job rebuilds the
HAT model on a schedule, and each IRIV picks it up after testing it. Everything stays
advisory and never controls a machine.
