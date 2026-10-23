"""Self-learning profiles: what "normal" looks like for every channel.

A profile is kept per channel, measurement kind, measurement settings and speed
band (10 % wide). It stores a running mean and variance, so learning costs the
same for the first sample and the millionth, and no history is re-read.

After the learning period, only samples judged normal update the profile. A
developing fault therefore cannot slowly teach the advisor that it is normal.
"""

import functools
import json
import math
import sqlite3

import numpy as np

MAX_SPECTRUM_BINS = 1600
SPEED_STEP = 1.10
FORGET_AFTER = 500  # older samples fade out after this many, so slow seasonal drift is followed
SPECTRUM_FLOOR = 0.05  # log10 units (0.5 dB): smallest spread treated as meaningful
WAVEFORM_NAMES = ("rms", "peak", "crest", "kurtosis", "skewness")
WAVEFORM_FLOORS = np.array([0.02, 0.02, 0.15, 0.3, 0.2])
METRIC_FLOOR = np.array([0.02])  # log10 units, about 5 %


def speed_band(rpm):
    if rpm is None or not math.isfinite(rpm) or rpm <= 0:
        return -1
    return int(round(math.log(rpm) / math.log(SPEED_STEP)))


@functools.lru_cache(maxsize=64)
def _pool_starts(lines):
    bins = min(lines, MAX_SPECTRUM_BINS)
    groups = np.array_split(np.arange(lines), bins)
    return np.array([g[0] for g in groups]), np.array([g.mean() + 1 for g in groups])


def bin_frequencies(bandwidth, lines):
    """Centre frequency (Hz) of each profile bin."""
    return _pool_starts(lines)[1] * bandwidth / lines


def spectrum_features(values, lines):
    """Log power per profile bin (DC removed, peaks kept by max-pooling)."""
    values = np.asarray(values, dtype=float)
    if not lines or values.size != lines + 1 or not np.all(np.isfinite(values)):
        raise ValueError("Spectrum does not match its line count")
    power = np.abs(values[1:])
    starts = _pool_starts(lines)[0]
    pooled = power if starts.size == power.size else np.maximum.reduceat(power, starts)
    floor = 1e-3 * float(np.median(pooled)) + 1e-20
    return np.log10(pooled + floor)


def waveform_features(values):
    """Overall level and impact indicators of a time waveform."""
    x = np.asarray(values, dtype=float)
    if x.size < 16 or not np.all(np.isfinite(x)):
        raise ValueError("Waveform too short or not finite")
    x = x - x.mean()
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 0:
        raise ValueError("Flat waveform")
    peak = float(np.max(np.abs(x)))
    at_peak = int(np.count_nonzero(np.abs(np.abs(x) - peak) <= 1e-9 * peak))
    return {
        "rms": rms, "peak": peak, "crest": peak / rms,
        "kurtosis": float(np.mean(x ** 4) / rms ** 4),
        "skewness": float(np.mean(x ** 3) / rms ** 3),
        "clipped": at_peak >= 3 and at_peak / x.size >= 0.01,
    }


def waveform_vector(features):
    return np.array([math.log10(features["rms"]), math.log10(features["peak"]),
                     features["crest"], features["kurtosis"], features["skewness"]])


class Profile:
    __slots__ = ("n", "first_ms", "last_ms", "mean", "var")

    def __init__(self, n=0, first_ms=None, last_ms=None, mean=None, var=None):
        self.n, self.first_ms, self.last_ms, self.mean, self.var = n, first_ms, last_ms, mean, var

    def learn(self, x, ms):
        x = np.asarray(x, dtype=float)
        if self.mean is None or self.mean.shape != x.shape:
            self.n, self.mean, self.var, self.first_ms = 0, x.copy(), np.zeros_like(x), ms
        self.n += 1
        a = 1.0 / min(self.n, FORGET_AFTER)
        delta = x - self.mean
        self.mean = self.mean + a * delta
        self.var = (1 - a) * (self.var + a * delta * delta)
        self.first_ms = ms if self.first_ms is None else min(self.first_ms, ms)
        self.last_ms = ms if self.last_ms is None else max(self.last_ms, ms)

    def ready(self, samples, hours):
        return (self.n >= samples and self.first_ms is not None
                and self.last_ms - self.first_ms >= hours * 3600_000)

    def zscores(self, x, floors):
        return (np.asarray(x, dtype=float) - self.mean) / np.sqrt(self.var + np.square(floors))


class ProfileStore:
    """Advisor-owned SQLite file. The dashboard historian is never written."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                gateway_id TEXT, channel TEXT, kind TEXT, signature TEXT, band INTEGER,
                n INTEGER, first_ms INTEGER, last_ms INTEGER, mean BLOB, var BLOB,
                PRIMARY KEY (gateway_id, channel, kind, signature, band));
            CREATE TABLE IF NOT EXISTS channel_state (
                gateway_id TEXT, channel TEXT, kind TEXT, state TEXT NOT NULL,
                PRIMARY KEY (gateway_id, channel, kind));
            CREATE TABLE IF NOT EXISTS watermarks (source TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
        """)
        self._profiles, self._states = {}, {}
        self._dirty_profiles, self._dirty_states = set(), set()
        for gid, ch, kind, body in self.conn.execute("SELECT gateway_id, channel, kind, state FROM channel_state"):
            self._states[(gid, ch, kind)] = json.loads(body)

    # profiles ---------------------------------------------------------
    def profile(self, gid, ch, kind, signature, band):
        key = (gid, ch, kind, signature, band)
        if key not in self._profiles:
            row = self.conn.execute(
                "SELECT n, first_ms, last_ms, mean, var FROM profiles WHERE gateway_id=? AND channel=? "
                "AND kind=? AND signature=? AND band=?", key).fetchone()
            if row:
                n, first, last, mean, var = row
                self._profiles[key] = Profile(n, first, last, np.frombuffer(mean, "<f8").copy(),
                                              np.frombuffer(var, "<f8").copy())
            else:
                self._profiles[key] = Profile()
        return self._profiles[key]

    def reference(self, gid, ch, kind, signature, band, samples, hours):
        """The ready profile for this speed, or the nearest neighbouring speed band."""
        for candidate in (band, band - 1, band + 1):
            profile = self.profile(gid, ch, kind, signature, candidate)
            if profile.ready(samples, hours):
                return profile
        return None

    def learn(self, gid, ch, kind, signature, band, x, ms):
        self.profile(gid, ch, kind, signature, band).learn(x, ms)
        self._dirty_profiles.add((gid, ch, kind, signature, band))

    # state ------------------------------------------------------------
    def state(self, gid, ch, kind):
        return self._states.setdefault((gid, ch, kind), {})

    def touch(self, gid, ch, kind):
        self._dirty_states.add((gid, ch, kind))

    def states(self):
        return self._states.items()

    def watermark(self, source):
        row = self.conn.execute("SELECT last_id FROM watermarks WHERE source=?", (source,)).fetchone()
        return row[0] if row else 0

    def set_watermark(self, source, last_id):
        self.conn.execute("INSERT OR REPLACE INTO watermarks VALUES (?,?)", (source, last_id))

    def flush(self):
        with self.conn:
            for key in self._dirty_profiles:
                p = self._profiles[key]
                self.conn.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  key + (p.n, p.first_ms, p.last_ms, p.mean.astype("<f8").tobytes(),
                                         p.var.astype("<f8").tobytes()))
            for key in self._dirty_states:
                self.conn.execute("INSERT OR REPLACE INTO channel_state VALUES (?,?,?,?)",
                                  key + (json.dumps(self._states[key]),))
        self._dirty_profiles.clear()
        self._dirty_states.clear()

    def forget(self, gid, ch):
        """Relearn a channel, for example after a repair changed its normal vibration."""
        with self.conn:
            self.conn.execute("DELETE FROM profiles WHERE gateway_id=? AND channel=?", (gid, ch))
            self.conn.execute("DELETE FROM channel_state WHERE gateway_id=? AND channel=?", (gid, ch))
        self._profiles = {k: v for k, v in self._profiles.items() if k[:2] != (gid, ch)}
        self._states = {k: v for k, v in self._states.items() if k[:2] != (gid, ch)}

    def close(self):
        self.flush()
        self.conn.close()
