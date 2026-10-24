"""Signal helpers for the history view and the IRIV AI: envelope, derived velocity, waveform figures."""

import math

import numpy as np

MIN_ENVELOPE_FS = 2000  # Hz; slower waveforms cannot carry bearing resonance
G = 9806.65  # mm/s² per g


def envelope_spectrum(values, fs):
    """Envelope (demodulated) spectrum of a waveform, for early bearing damage.

    Bearing defects make short impacts that ring the structure at high frequency.
    Band-passing that band (10-40 % of the sampling rate) and taking its envelope
    shows the impact rate (BPFO, BPFI, ...). Overlapping segments are averaged
    (Welch) so a young defect is not lost in line-to-line scatter.
    Returns (frequencies Hz, power) up to 1 kHz.
    """
    x = np.asarray(values, dtype=float)
    if not fs or fs < MIN_ENVELOPE_FS:
        raise ValueError(f"Waveform sampled at {fs or 0:g} Hz; envelope analysis needs {MIN_ENVELOPE_FS} Hz or more")
    if x.size < 1024 or not np.all(np.isfinite(x)):
        raise ValueError("Waveform too short for envelope analysis")
    x = x - x.mean()
    spectrum = np.fft.fft(x)
    freqs = np.fft.fftfreq(x.size, 1.0 / fs)
    band = (freqs >= 0.1 * fs) & (freqs <= 0.4 * fs)  # positive band only: analytic signal of the band
    envelope = np.abs(np.fft.ifft(np.where(band, 2 * spectrum, 0)))
    envelope -= envelope.mean()
    segment = 1 << int(math.log2(min(fs / 2, x.size / 4)))
    windows = np.lib.stride_tricks.sliding_window_view(envelope, segment)[::segment // 2]
    power = np.mean(np.abs(np.fft.rfft(windows * np.hanning(segment), axis=1)) ** 2, axis=0)
    f = np.fft.rfftfreq(segment, 1.0 / fs)
    keep = (f > 0) & (f <= min(1000.0, 0.15 * fs))
    return f[keep], power[keep]


def velocity_from_acceleration(freqs, power, min_hz=2.0):
    """Velocity amplitude (mm/s) from an acceleration power spectrum in g².

    amplitude = sqrt(power) g, velocity = amplitude x 9806.65 / (2 pi f). Lines below
    min_hz are set to zero, because integration blows up near 0 Hz.
    """
    freqs, power = np.asarray(freqs, dtype=float), np.asarray(power, dtype=float)
    velocity = np.zeros_like(power)
    ok = freqs >= min_hz
    velocity[ok] = np.sqrt(np.abs(power[ok])) * G / (2 * np.pi * freqs[ok])
    return velocity


def waveform_stats(values):
    x = np.asarray(values, dtype=float)
    if x.size == 0:
        return {}
    x = x - x.mean()
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))
    return {"rms": rms, "peak": peak, "peak_to_peak": float(x.max() - x.min()),
            "crest": peak / rms if rms else 0.0,
            "kurtosis": float(np.mean(x ** 4) / rms ** 4) if rms else 0.0}


def spectrum_amplitude(freqs, power, sensor_type, envelope=False):
    """Spectrum power in the unit people read: (amplitude, unit, axis title).

    Acceleration in g becomes velocity in mm/s (ISO vibration severity is judged in mm/s).
    A velocity sensor stays in mm/s. Envelope spectra have no physical unit (unit ""): read them
    against their own learned normal.
    """
    power = np.abs(np.asarray(power, dtype=float))
    sensor = str(sensor_type or "").lower()
    accel = "accel" in sensor or sensor.endswith("(g)")
    if envelope:
        return np.sqrt(power), "", "Envelope level (bearing impacts, relative)"
    if accel:
        return velocity_from_acceleration(freqs, power), "mm/s", "Velocity (mm/s, from the acceleration spectrum)"
    if "vel" in sensor:
        return np.sqrt(power), "mm/s", "Velocity (mm/s)"
    return np.sqrt(power), "sensor units", "Amplitude (sensor units)"
