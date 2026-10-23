#!/usr/bin/env python3
"""Train a small spectrum autoencoder on healthy recordings and export it to ONNX.

Run this on a PC, not on the IRIV Edge AI:
    pip install torch onnx numpy
    python train_autoencoder.py spectra.npy --out model/

Input is the file made on the device with:
    python3 ai_advisor.py --data-dir <data> --export-dataset spectra.npy
Record only while the machine is known to be healthy; the model learns "normal".

Outputs in --out:
    spectrum_ae.onnx   compile this with the Hailo Dataflow Compiler (README.md)
    spectrum_ae.json   normalisation; copy to the device next to the .hef
    calibration.npy    normalised samples for the compiler's quantisation step
"""

import argparse
import json
from pathlib import Path

import numpy as np


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("dataset", help=".npy file from ai_advisor.py --export-dataset")
    parser.add_argument("--out", default="model")
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    import torch
    from torch import nn

    torch.manual_seed(args.seed)
    data = np.load(args.dataset).astype(np.float32)
    if data.ndim != 2 or len(data) < 20:
        raise SystemExit("Need at least 20 recorded spectra; record longer while the machine is healthy.")
    mean, std = data.mean(axis=0), np.maximum(data.std(axis=0), 0.05)
    normalised = (data - mean) / std
    bins = data.shape[1]

    model = nn.Sequential(
        nn.Linear(bins, 64), nn.ReLU(), nn.Linear(64, 16), nn.ReLU(),
        nn.Linear(16, 64), nn.ReLU(), nn.Linear(64, bins),
    )
    optimiser = torch.optim.Adam(model.parameters(), lr=1e-3)
    tensor = torch.from_numpy(normalised)
    for epoch in range(args.epochs):
        permutation = torch.randperm(len(tensor))
        for start in range(0, len(tensor), 32):
            batch = tensor[permutation[start:start + 32]]
            loss = nn.functional.mse_loss(model(batch), batch)
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
        if epoch % 50 == 0 or epoch == args.epochs - 1:
            print(f"epoch {epoch}: loss {loss.item():.4f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.eval()
    torch.onnx.export(model, torch.zeros(1, bins), out / "spectrum_ae.onnx",
                      input_names=["spectrum"], output_names=["reconstruction"], opset_version=13,
                      dynamo=False)
    (out / "spectrum_ae.json").write_text(json.dumps({"mean": mean.tolist(), "std": std.tolist()}))
    np.save(out / "calibration.npy", normalised[: min(len(normalised), 1024)])
    print(f"Wrote {out / 'spectrum_ae.onnx'}, {out / 'spectrum_ae.json'} and {out / 'calibration.npy'}")


if __name__ == "__main__":
    main()
