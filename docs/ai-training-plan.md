# Plan: teaching the IRIV Edge AI from real data

Status: agreed, not started (2026-10-25). Nothing described here is built yet; today
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
| Training, at first | Your PC, or Google Colab / Kaggle notebooks | free |
| Training and HAT build, automatic | GitHub Actions (public repository) | free |
| Later, if wanted | Compute Engine VM or Vertex AI job, switched off after each run | about $1–5 a month |

## Steps to build (later)

1. Collect machine cases with all channels, the opt-in setting, a download to look at
   them first, and the uploader to Cloud Storage.
2. Retrain the small model on the Pi's processor overnight from confirmed cases.
3. GitHub Actions job: train, build the HAT files (hailo8 and hailo8l), publish them.
4. On the IRIV: download new models, run them in shadow mode beside the current one,
   switch only if they agree better with people's feedback, roll back automatically.

## One-time steps for the owner

1. Create a Hailo Developer Zone account and download the Dataflow Compiler.
2. Check that Hailo's licence allows running the compiler on GitHub's or Google's machines.
3. Keep the compiler in a private place and add the access key as a GitHub secret.
4. Merge to `main` and push release tags.
5. Each customer switches sharing on.

After that it runs by itself: the Pi learns, cases are collected, the job rebuilds the
HAT model on a schedule, and each IRIV picks it up after testing it. Everything stays
advisory and never controls a machine.
