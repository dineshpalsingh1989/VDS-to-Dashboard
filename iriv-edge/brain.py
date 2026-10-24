"""Neural second opinion for the IRIV Edge AI advisor, run on the Hailo AI HAT+ when it is fitted.

The advisor already knows what is normal for each machine (its learned profile).
This network looks at HOW a spectrum changed from that normal, laid out in
multiples of running speed (orders), and names the fault pattern:

    input  : 256 order bins of the normal spectrum's rise (dB / 20, 0-16x running speed)
           + 256 order bins of the envelope spectrum's rise
    output : probabilities for normal, unbalance, misalignment, looseness,
             outer race, inner race, rolling element

Because the input is "change from this machine's own normal, in orders", one
network works for every machine and speed. It is small (512-64-32-7, about
35 000 weights) so it runs in microseconds on the Hailo chip and in well under
a millisecond on the CM5 processor.

Backends, chosen at start-up:
  * hailo : a compiled model in models/ on the Hailo AI HAT+ through HailoRT (hailo_platform).
            vibration_brain.hef if you compiled one yourself, else vibration_brain_hailo8.hef
            (26 TOPS HAT) or vibration_brain_hailo8l.hef (13 TOPS HAT), whichever the chip accepts;
  * cpu   : the same weights from models/vibration_brain.npz with numpy.
The HEF is compiled from the ONNX export of the same weights (see iriv-edge/README.md, "The AI HAT+"),
so both give the same answer apart from the HAT's 8-bit rounding.

Advisory only: the result is shown next to the rule-based diagnosis and never
issues a machine command.
"""

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("vds-ai-advisor")
HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "models"
HEF_NAMES = ("vibration_brain.hef", "vibration_brain_hailo8.hef", "vibration_brain_hailo8l.hef")
ORDERS_MAX, ORDER_BINS = 16.0, 256
INPUT_SIZE = 2 * ORDER_BINS
CLASSES = ("normal", "unbalance", "misalignment", "looseness", "outer_race", "inner_race", "rolling_element")
NAMES = {"normal": "Normal running", "unbalance": "Unbalance", "misalignment": "Shaft misalignment",
         "looseness": "Mechanical looseness", "outer_race": "Bearing outer race defect",
         "inner_race": "Bearing inner race defect", "rolling_element": "Bearing rolling element defect"}
# Which rule-based faults (knowledge/faults.json) each class agrees with
RULE_IDS = {"unbalance": ("unbalance",), "misalignment": ("misalignment",), "looseness": ("looseness",),
            "outer_race": ("bearing_outer_race", "envelope_outer_race"),
            "inner_race": ("bearing_inner_race", "envelope_inner_race"),
            "rolling_element": ("bearing_rolling_element", "envelope_rolling_element")}


def order_rise(freqs, rise_db, rpm):
    """Largest rise (dB, clipped to -10..30, divided by 20) in each 1/16-order bin from 0 to 16x running speed."""
    out = np.zeros(ORDER_BINS, dtype=np.float32)
    if not rpm or rpm <= 0:
        return out
    orders = np.asarray(freqs, dtype=float) / (rpm / 60.0)
    index = np.floor(orders / (ORDERS_MAX / ORDER_BINS)).astype(int)
    valid = (index >= 0) & (index < ORDER_BINS)
    values = np.clip(np.asarray(rise_db, dtype=float), -10, 30) / 20.0
    np.maximum.at(out, index[valid], values[valid].astype(np.float32))
    return out


def features(spectrum_orders=None, envelope_orders=None):
    parts = [np.zeros(ORDER_BINS, np.float32) if part is None else np.asarray(part, np.float32)
             for part in (spectrum_orders, envelope_orders)]
    return np.concatenate(parts)


def softmax(z):
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def forward(weights, x):
    """The network in numpy: two hidden ReLU layers and softmax."""
    h = np.maximum(0, x @ weights["w1"] + weights["b1"])
    h = np.maximum(0, h @ weights["w2"] + weights["b2"])
    return softmax(h @ weights["w3"] + weights["b3"])


class CpuBackend:
    name = "cpu"

    def __init__(self, weights):
        self.weights = weights

    def run(self, x):
        return forward(self.weights, x)

    def close(self):
        pass


class HailoBackend:
    """The compiled network on the Hailo AI HAT+, through the HailoRT Python API (hailo_platform)."""
    name = "hailo"

    def __init__(self, hef_path, hpf=None):
        if hpf is None:
            import hailo_platform as hpf  # installed with HailoRT on the IRIV Edge AI
        self.hpf = hpf
        self.hef = hpf.HEF(str(hef_path))
        self.device = hpf.VDevice()
        params = hpf.ConfigureParams.create_from_hef(self.hef, interface=hpf.HailoStreamInterface.PCIe)
        self.group = self.device.configure(self.hef, params)[0]
        self.group_params = self.group.create_params()
        self.input_info = self.hef.get_input_vstream_infos()[0]
        self.output_info = self.hef.get_output_vstream_infos()[0]
        self.in_params = self._vstream_params(hpf.InputVStreamParams)
        self.out_params = self._vstream_params(hpf.OutputVStreamParams)

    def _vstream_params(self, kind):
        try:  # HailoRT 4.19 and earlier
            return kind.make_from_network_group(self.group, quantized=False, format_type=self.hpf.FormatType.FLOAT32)
        except TypeError:  # newer HailoRT dropped the quantized argument
            return kind.make_from_network_group(self.group, format_type=self.hpf.FormatType.FLOAT32)

    def run(self, x):
        batch = np.asarray(x, np.float32).reshape((-1,) + tuple(self.input_info.shape))
        with self.group.activate(self.group_params), \
                self.hpf.InferVStreams(self.group, self.in_params, self.out_params) as pipeline:
            out = np.asarray(pipeline.infer({self.input_info.name: batch})[self.output_info.name], dtype=float)
        out = out.reshape(len(batch), -1)
        # Softmax may run on the chip or be left to the host, depending on how the HEF was compiled
        return out if np.allclose(out.sum(axis=1), 1, atol=0.05) and out.min() >= 0 else softmax(out)

    def close(self):
        try:
            self.device.release()
        except Exception:  # noqa: BLE001  best effort when the service stops
            pass


class Brain:
    """Loads the network once and answers with class probabilities; falls back to the CPU when there is no HAT."""

    def __init__(self, model_dir=MODEL_DIR, use_hailo=True, hpf=None):
        self.model_dir = Path(model_dir)
        self.backend, self.status, self.version = None, "No neural model installed.", None
        npz = self.model_dir / "vibration_brain.npz"
        if not npz.exists():
            return
        with np.load(npz, allow_pickle=False) as data:
            weights = {k: data[k].astype(np.float32) for k in ("w1", "b1", "w2", "b2", "w3", "b3")}
            self.version = str(data["version"]) if "version" in data else "1"
        hefs = [self.model_dir / name for name in HEF_NAMES if (self.model_dir / name).exists()]
        for hef in hefs if use_hailo else []:
            try:  # a HEF compiled for the other Hailo chip is refused; then try the next one
                self.backend = HailoBackend(hef, hpf)
                self.status = f"Neural model running on the Hailo AI HAT+ ({hef.name})."
                break
            except Exception as error:  # noqa: BLE001  no HAT, no driver or a HEF for another chip
                log.info("Hailo AI HAT+ not used with %s (%s).", hef.name, error)
                self.status = f"Neural model running on the CPU (Hailo AI HAT+ not available: {error})."
        if self.backend is None:
            self.backend = CpuBackend(weights)
            if not hefs:
                self.status = "Neural model running on the CPU (no compiled Hailo model in models/)."
        self.cpu = CpuBackend(weights)

    @property
    def ready(self):
        return self.backend is not None

    def predict(self, x):
        """Class probabilities for one feature vector, best first: [(class, probability)]."""
        x = np.asarray(x, np.float32).reshape(1, INPUT_SIZE)
        try:
            probs = self.backend.run(x)[0]
        except Exception as error:  # noqa: BLE001  a HAT fault must never stop the advisor
            log.warning("Hailo inference failed (%s); using the CPU from now on.", error)
            self.backend.close()
            self.backend, self.status = self.cpu, f"Neural model moved to the CPU after a Hailo error: {error}."
            probs = self.backend.run(x)[0]
        order = np.argsort(probs)[::-1]
        return [(CLASSES[i], round(float(probs[i]), 3)) for i in order]

    def close(self):
        if self.backend:
            self.backend.close()


def check(cases=70):
    """On the IRIV Edge AI: which backend runs, and does the HAT agree with the CPU model?"""
    import sys
    sys.path.insert(0, str(HERE / "hailo"))
    from train_brain import dataset

    brain = Brain()
    print(brain.status)
    if not brain.ready:
        return 1
    x, y = dataset(max(1, cases // len(CLASSES)), seed=5)
    used = np.array([CLASSES.index(brain.predict(row)[0][0]) for row in x])
    cpu = brain.cpu.run(x).argmax(axis=1)
    print(f"Backend: {brain.backend.name}. Correct on synthetic cases: {np.mean(used == y):.0%}. "
          f"Same answer as the CPU model: {np.mean(used == cpu):.0%}.")
    brain.close()
    return 0


if __name__ == "__main__":
    import argparse

    argparse.ArgumentParser(description="Check the neural model and the Hailo AI HAT+.").parse_args()
    raise SystemExit(check())
