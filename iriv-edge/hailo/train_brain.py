#!/usr/bin/env python3
"""Train the advisor's neural model on a PC and export it for the Hailo AI HAT+.

    python iriv-edge/hailo/train_brain.py            # writes iriv-edge/models/vibration_brain.{npz,onnx,json}

Training data is synthetic: fault patterns drawn the way the advisor sees them,
as the rise of each order bin over the machine's learned normal. Bearing defect
frequencies are drawn over the range real ball bearings cover, so the network
does not depend on one bearing size. Feedback that people confirm in the
dashboard (learning.db labels) can be added later with --labels.

Plain numpy, fixed random seed: the same command always gives the same model.
Then compile the ONNX file to a HEF with hailo/compile_hef.py (needs Hailo's
Dataflow Compiler on an x86 Linux PC) and copy the HEF to iriv-edge/models/.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from brain import CLASSES, INPUT_SIZE, MODEL_DIR, ORDER_BINS, ORDERS_MAX, forward

VERSION = "2026.10-synthetic-2"
STEP = ORDERS_MAX / ORDER_BINS


def peak(vector, order, rise_db, rng):
    """A rise at one order, smeared over a neighbouring bin as speed wanders a little."""
    order *= 1 + rng.normal(0, 0.004)
    i = int(order / STEP)
    if 0 <= i < ORDER_BINS:
        vector[i] = max(vector[i], rise_db)
        j = i + (1 if (order / STEP - i) > 0.5 else -1)
        if 0 <= j < ORDER_BINS and rng.random() < 0.5:
            vector[j] = max(vector[j], rise_db - rng.uniform(2, 8))


def sample(label, rng):
    """(spectrum rise, envelope rise) in dB for one synthetic case."""
    spec = rng.normal(0, 0.8, ORDER_BINS).clip(-3, 3)
    env = rng.normal(0, 0.8, ORDER_BINS).clip(-3, 3)
    for vector in (spec, env):  # harmless small changes that must not count as faults
        for _ in range(rng.integers(0, 4)):
            peak(vector, rng.uniform(0.3, ORDERS_MAX - 0.5), rng.uniform(2, 5), rng)
    strength = rng.uniform(6, 26)
    if label == "normal" and rng.random() < 0.3:  # one modest unexplained change
        peak(spec if rng.random() < 0.5 else env, rng.uniform(0.5, 15), rng.uniform(4, 7), rng)
    elif label == "unbalance":
        peak(spec, 1, strength, rng)
        if rng.random() < 0.5:
            peak(spec, 2, rng.uniform(0, 4), rng)
    elif label == "misalignment":
        peak(spec, 2, strength, rng)
        peak(spec, 1, rng.uniform(0, strength * 0.6), rng)
        if rng.random() < 0.7:
            peak(spec, 3, rng.uniform(3, strength * 0.8), rng)
    elif label == "looseness":
        harmonics = rng.choice(np.arange(1, 11), size=rng.integers(5, 10), replace=False)
        for k in harmonics:
            peak(spec, k, rng.uniform(4, 16), rng)
        if rng.random() < 0.6:
            for k in rng.choice(np.arange(1, 8), size=rng.integers(1, 4), replace=False):
                peak(spec, k + 0.5, rng.uniform(3, 10), rng)
    elif label == "outer_race":
        bpfo = rng.uniform(2.8, 5.3)
        for k in range(1, rng.integers(2, 7)):  # harmonics of similar size; the 2nd is often the strongest
            peak(env, k * bpfo, strength - 2 * (k - 1) + rng.normal(0, 4), rng)
        if rng.random() < 0.5:
            for k in range(1, 3):
                peak(spec, k * bpfo, rng.uniform(3, 12), rng)
    elif label == "inner_race":
        bpfi = rng.uniform(4.5, 8.0)
        for k in range(1, rng.integers(2, 4)):
            peak(env, k * bpfi, strength - 4 * (k - 1) + rng.normal(0, 2), rng)
            sides = [side for side in (-1, 1) if rng.random() < 0.75] or [rng.choice((-1, 1))]
            for side in sides:  # sidebands one running speed away: the race turns through the load zone
                peak(env, k * bpfi + side, rng.uniform(4, strength * 0.7), rng)
        if rng.random() < 0.6:
            peak(env, 1, rng.uniform(3, 10), rng)
    elif label == "rolling_element":
        bsf, ftf = rng.uniform(1.4, 3.2), rng.uniform(0.37, 0.43)
        peak(env, 2 * bsf, strength, rng)
        if rng.random() < 0.7:
            peak(env, bsf, rng.uniform(3, strength * 0.8), rng)
        if rng.random() < 0.6:
            peak(env, 4 * bsf, rng.uniform(3, strength * 0.7), rng)
        for side in (-1, 1):  # sidebands at the cage frequency
            if rng.random() < 0.7:
                peak(env, 2 * bsf + side * ftf, rng.uniform(3, strength * 0.6), rng)
    return spec, env


def dataset(per_class, seed):
    rng = np.random.default_rng(seed)
    x, y = [], []
    for label_index, label in enumerate(CLASSES):
        for _ in range(per_class):
            spec, env = sample(label, rng)
            x.append(np.concatenate([np.clip(spec, -10, 30), np.clip(env, -10, 30)]) / 20.0)
            y.append(label_index)
    return np.asarray(x, np.float32), np.asarray(y)


def train(x, y, hidden=(64, 32), epochs=30, batch=128, rate=2e-3, seed=1):
    """Adam on cross-entropy with a little weight decay."""
    rng = np.random.default_rng(seed)
    sizes = (INPUT_SIZE, *hidden, len(CLASSES))
    w = {}
    for n, (a, b) in enumerate(zip(sizes, sizes[1:], strict=False), start=1):
        w[f"w{n}"] = (rng.normal(0, np.sqrt(2 / a), (a, b))).astype(np.float32)
        w[f"b{n}"] = np.zeros(b, np.float32)
    m = {k: np.zeros_like(v) for k, v in w.items()}
    v2 = {k: np.zeros_like(v) for k, v in w.items()}
    step = 0
    onehot = np.eye(len(CLASSES), dtype=np.float32)[y]
    for _epoch in range(epochs):
        order = rng.permutation(len(x))
        for start in range(0, len(x), batch):
            idx = order[start:start + batch]
            xb, yb = x[idx], onehot[idx]
            z1 = xb @ w["w1"] + w["b1"]
            h1 = np.maximum(0, z1)
            z2 = h1 @ w["w2"] + w["b2"]
            h2 = np.maximum(0, z2)
            logits = h2 @ w["w3"] + w["b3"]
            p = np.exp(logits - logits.max(axis=1, keepdims=True))
            p /= p.sum(axis=1, keepdims=True)
            g3 = (p - yb) / len(xb)
            grads = {"w3": h2.T @ g3, "b3": g3.sum(0)}
            g2 = (g3 @ w["w3"].T) * (z2 > 0)
            grads.update(w2=h1.T @ g2, b2=g2.sum(0))
            g1 = (g2 @ w["w2"].T) * (z1 > 0)
            grads.update(w1=xb.T @ g1, b1=g1.sum(0))
            step += 1
            for k in w:
                grad = grads[k] + (5e-4 * w[k] if k.startswith("w") else 0)
                m[k] = 0.9 * m[k] + 0.1 * grad
                v2[k] = 0.999 * v2[k] + 0.001 * grad * grad
                w[k] -= rate * (m[k] / (1 - 0.9 ** step)) / (np.sqrt(v2[k] / (1 - 0.999 ** step)) + 1e-8)
    return w


def accuracy(weights, x, y):
    return float(np.mean(forward(weights, x).argmax(axis=1) == y))


def export_onnx(weights, path):
    """The same network as ONNX (Gemm, Relu, Softmax): the input for Hailo's Dataflow Compiler."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    nodes, inits, previous = [], [], "features"
    for n in (1, 2, 3):
        inits += [numpy_helper.from_array(weights[f"w{n}"], f"w{n}"), numpy_helper.from_array(weights[f"b{n}"], f"b{n}")]
        out = f"dense{n}"
        nodes.append(helper.make_node("Gemm", [previous, f"w{n}", f"b{n}"], [out], name=out))
        if n < 3:
            nodes.append(helper.make_node("Relu", [out], [f"relu{n}"], name=f"relu{n}"))
            previous = f"relu{n}"
        else:
            previous = out
    nodes.append(helper.make_node("Softmax", [previous], ["probabilities"], name="softmax", axis=1))
    graph = helper.make_graph(nodes, "vibration_brain", [helper.make_tensor_value_info("features", TensorProto.FLOAT,
                                                                                      [1, INPUT_SIZE])],
                              [helper.make_tensor_value_info("probabilities", TensorProto.FLOAT, [1, len(CLASSES)])],
                              inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="vds-to-dashboard")
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, str(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-class", type=int, default=6000)
    parser.add_argument("--out", type=Path, default=MODEL_DIR)
    parser.add_argument("--no-onnx", action="store_true", help="skip the ONNX export (no onnx package)")
    args = parser.parse_args(argv)
    x, y = dataset(args.per_class, seed=2026)
    test_x, test_y = dataset(400, seed=7)
    weights = train(x, y)
    result = {"version": VERSION, "classes": list(CLASSES), "input": f"{ORDER_BINS} spectrum + {ORDER_BINS} envelope "
              f"order bins, rise over normal in dB / 20, 0-{ORDERS_MAX:g}x running speed",
              "train_accuracy": round(accuracy(weights, x, y), 4), "test_accuracy": round(accuracy(weights, test_x, test_y), 4)}
    args.out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out / "vibration_brain.npz", version=np.array(VERSION), **weights)
    (args.out / "vibration_brain.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    if not args.no_onnx:
        export_onnx(weights, args.out / "vibration_brain.onnx")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
