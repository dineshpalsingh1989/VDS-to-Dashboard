"""Neural second opinion: the trained model, the Hailo AI HAT+ glue (with a stand-in HailoRT) and the CPU fallback."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ADVISOR_DIR = Path(__file__).resolve().parent.parent / "iriv-edge"
sys.path.insert(0, str(ADVISOR_DIR))
sys.path.insert(0, str(ADVISOR_DIR / "hailo"))
import brain
from train_brain import dataset


class FakeHailo:
    """Just enough of hailo_platform to drive HailoBackend the way HailoRT does."""

    def __init__(self, weights, fail=False, raw_scores=False, refuse=()):
        self.weights, self.fail, self.raw_scores, self.calls = weights, fail, raw_scores, []
        self.refuse, self.loaded = refuse, None
        test = self

        class Info(SimpleNamespace):
            pass

        class HEF:
            def __init__(self, path):
                test.calls.append(("hef", Path(path).name))
                test.loaded = Path(path).name

            def get_input_vstream_infos(self):
                return [Info(name="vibration_brain/input_layer1", shape=(brain.INPUT_SIZE,))]

            def get_output_vstream_infos(self):
                return [Info(name="vibration_brain/fc3", shape=(len(brain.CLASSES),))]

        class Group:
            def create_params(self):
                return "group-params"

            def activate(self, params):
                test.calls.append(("activate", params))
                return _Context()

        class VDevice:
            def configure(self, hef, params):
                if test.loaded in test.refuse:
                    raise RuntimeError("HEF compiled for another Hailo chip")
                test.calls.append(("configure", params))
                return [Group()]

            def release(self):
                test.calls.append(("release",))

        class Params:
            @staticmethod
            def make_from_network_group(group, format_type=None, **kwargs):
                if "quantized" in kwargs:
                    raise TypeError("newer HailoRT has no quantized argument")
                return format_type

        class Pipeline(_Context):
            def __init__(self, group, inputs, outputs):
                pass

            def infer(self, data):
                if test.fail:
                    raise RuntimeError("PCIe link lost")
                x = data["vibration_brain/input_layer1"]
                test.calls.append(("infer", x.shape))
                if test.raw_scores:  # softmax left to the host
                    h = np.maximum(0, x @ test.weights["w1"] + test.weights["b1"])
                    h = np.maximum(0, h @ test.weights["w2"] + test.weights["b2"])
                    return {"vibration_brain/fc3": h @ test.weights["w3"] + test.weights["b3"]}
                return {"vibration_brain/fc3": brain.forward(test.weights, x)}

        self.HEF, self.VDevice, self.InferVStreams = HEF, VDevice, Pipeline
        self.InputVStreamParams = self.OutputVStreamParams = Params
        self.ConfigureParams = SimpleNamespace(create_from_hef=lambda hef, interface: "configure-params")
        self.HailoStreamInterface = SimpleNamespace(PCIe="pcie")
        self.FormatType = SimpleNamespace(FLOAT32="float32")


class _Context:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.x, cls.y = dataset(150, seed=99)
        with np.load(brain.MODEL_DIR / "vibration_brain.npz") as data:
            cls.weights = {k: data[k].astype(np.float32) for k in ("w1", "b1", "w2", "b2", "w3", "b3")}

    def model_dir(self, hef=False, names=("vibration_brain.hef",)):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        (path / "vibration_brain.npz").write_bytes((brain.MODEL_DIR / "vibration_brain.npz").read_bytes())
        for name in names if hef else ():
            (path / name).write_bytes(b"stand-in HEF")
        return path

    def test_shipped_model_names_fault_patterns(self):
        predicted = brain.forward(self.weights, self.x).argmax(axis=1)
        self.assertGreaterEqual(float(np.mean(predicted == self.y)), 0.9)
        machine = [brain.CLASSES.index(c) for c in ("unbalance", "misalignment", "looseness")]
        keep = np.isin(self.y, machine)
        self.assertGreaterEqual(float(np.mean(predicted[keep] == self.y[keep])), 0.97)

    def test_order_bins(self):
        bins = brain.order_rise([24.75, 88.0, 400.0], [12.0, 20.0, 40.0], 1485)
        self.assertAlmostEqual(float(bins[16]), 0.6)          # 1x lands in bin 16 (1/16 order bins)
        self.assertAlmostEqual(float(bins[int(3.5556 * 16)]), 1.0)
        self.assertEqual(float(bins.max()), 1.0)              # 16.2x is past the last bin and dropped; 30 dB clip
        self.assertEqual(float(brain.order_rise([10.0], [9.0], None).sum()), 0.0)

    def test_cpu_when_there_is_no_hef(self):
        model = brain.Brain(self.model_dir())
        self.assertEqual(model.backend.name, "cpu")
        self.assertIn("no compiled Hailo model", model.status)
        top = model.predict(self.x[0])
        self.assertAlmostEqual(sum(p for _c, p in top), 1, places=2)

    def test_runs_on_the_hailo_hat_when_fitted(self):
        for raw in (False, True):
            fake = FakeHailo(self.weights, raw_scores=raw)
            model = brain.Brain(self.model_dir(hef=True), hpf=fake)
            self.assertEqual(model.backend.name, "hailo")
            self.assertIn("Hailo AI HAT+", model.status)
            row = self.x[self.y == brain.CLASSES.index("unbalance")][0]
            self.assertEqual(model.predict(row)[0][0], "unbalance")
            self.assertIn(("infer", (1, brain.INPUT_SIZE)), fake.calls)
            self.assertIn(("activate", "group-params"), fake.calls)

    def test_picks_the_hef_this_chip_accepts(self):
        names = ("vibration_brain_hailo8.hef", "vibration_brain_hailo8l.hef")
        fake = FakeHailo(self.weights, refuse=("vibration_brain_hailo8.hef",))  # a 13 TOPS HAT
        model = brain.Brain(self.model_dir(hef=True, names=names), hpf=fake)
        self.assertEqual(model.backend.name, "hailo")
        self.assertIn("vibration_brain_hailo8l.hef", model.status)

    def test_falls_back_to_the_cpu_if_the_hat_fails(self):
        fake = FakeHailo(self.weights, fail=True)
        model = brain.Brain(self.model_dir(hef=True), hpf=fake)
        row = self.x[self.y == brain.CLASSES.index("looseness")][0]
        self.assertEqual(model.predict(row)[0][0], "looseness")
        self.assertEqual(model.backend.name, "cpu")
        self.assertIn("after a Hailo error", model.status)
        self.assertIn(("release",), fake.calls)

    def test_no_driver_means_cpu(self):
        model = brain.Brain(self.model_dir(hef=True), hpf=SimpleNamespace())  # HailoRT missing pieces
        self.assertEqual(model.backend.name, "cpu")
        self.assertIn("not available", model.status)

    def test_onnx_export_matches_the_numpy_model(self):
        try:
            import onnxruntime
        except ImportError:
            self.skipTest("onnxruntime is not installed")
        session = onnxruntime.InferenceSession(str(brain.MODEL_DIR / "vibration_brain.onnx"))
        for row in self.x[:20]:
            out = session.run(None, {"features": row.reshape(1, -1)})[0]
            np.testing.assert_allclose(out, brain.forward(self.weights, row.reshape(1, -1)), atol=1e-5)


if __name__ == "__main__":
    unittest.main()
