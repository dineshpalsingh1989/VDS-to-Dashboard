#!/usr/bin/env python3
"""Compile the neural model's ONNX file into a HEF for the Hailo AI HAT+.

Run on an x86-64 Linux PC with Hailo's Dataflow Compiler (hailo_sdk_client) installed
from the Hailo Developer Zone; it does not run on the Raspberry Pi itself.

    python iriv-edge/hailo/compile_hef.py               # both chips: models/vibration_brain_hailo8{,l}.hef
    python iriv-edge/hailo/compile_hef.py --arch hailo8l   # 13 TOPS AI HAT+ only

Or run everything in Docker with one command: bash iriv-edge/hailo/build_hef.sh

Steps: parse the ONNX graph, quantise it to 8 bits with synthetic calibration
cases from train_brain.py, compile, and write iriv-edge/models/vibration_brain_<arch>.hef.
brain.py on the IRIV picks the one its chip accepts, so commit both and every
download of the repository carries them.
Softmax is left to the host (brain.py applies it when the chip returns raw scores).

The Dataflow Compiler API changes between releases; this script follows the
ClientRunner flow (translate_onnx_model, optimize, compile). Check it against the
documentation of your compiler version, and compare the HEF's answers with the
CPU model with "python3 iriv-edge/brain.py" on the IRIV Edge AI (see iriv-edge/README.md).
"""

import argparse
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from brain import INPUT_SIZE, MODEL_DIR
from train_brain import dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", choices=("all", "hailo8l", "hailo8"), default="all")
    parser.add_argument("--onnx", type=Path, default=MODEL_DIR / "vibration_brain.onnx")
    parser.add_argument("--out-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--calibration", type=int, default=1024, help="number of calibration cases")
    args = parser.parse_args(argv)

    from hailo_sdk_client import ClientRunner  # Hailo Dataflow Compiler

    x, _y = dataset(max(1, args.calibration // 7 + 1), seed=11)
    calibration = x[: args.calibration].reshape(-1, INPUT_SIZE).astype(np.float32)

    for arch in ("hailo8", "hailo8l") if args.arch == "all" else (args.arch,):
        out = args.out_dir / f"vibration_brain_{arch}.hef"
        runner = ClientRunner(hw_arch=arch)
        runner.translate_onnx_model(str(args.onnx), "vibration_brain", start_node_names=["dense1"],
                                    end_node_names=["dense3"], net_input_shapes={"features": [1, INPUT_SIZE]})
        runner.optimize(calibration)
        out.write_bytes(runner.compile())
        print(f"Wrote {out} for {arch}.")
    print("Commit the HEF files in iriv-edge/models/; the IRIV Edge AI uses them after: "
          "sudo systemctl restart vds-ai-advisor")


if __name__ == "__main__":
    main()
