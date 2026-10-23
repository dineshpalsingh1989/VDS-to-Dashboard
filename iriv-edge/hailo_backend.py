"""Run a spectrum autoencoder on the Raspberry Pi AI HAT+ (Hailo-8L / Hailo-8).

Requires HailoRT and its Python binding on the device:
    sudo apt install hailo-all
The .hef file comes from train_autoencoder.py plus the Hailo Dataflow Compiler
(see README.md). This module only performs inference; it has no outputs other
than the reconstruction error it returns.
"""

import json

import numpy as np


class HailoAutoencoder:
    name = "Hailo autoencoder"

    def __init__(self, hef_path, meta_path):
        # Imported here so the CPU backend works on machines without HailoRT.
        from hailo_platform import (HEF, ConfigureParams, FormatType, HailoStreamInterface,
                                    InferVStreams, InputVStreamParams, OutputVStreamParams, VDevice)

        with open(meta_path, encoding="utf-8") as stream:
            meta = json.load(stream)
        self.mean = np.asarray(meta["mean"], dtype=np.float32)
        self.std = np.asarray(meta["std"], dtype=np.float32)

        self._hef = HEF(hef_path)
        self._device = VDevice()
        params = ConfigureParams.create_from_hef(self._hef, interface=HailoStreamInterface.PCIe)
        self._group = self._device.configure(self._hef, params)[0]
        self._group_params = self._group.create_params()
        self._input = self._hef.get_input_vstream_infos()[0]
        self._output = self._hef.get_output_vstream_infos()[0]
        self._in_params = InputVStreamParams.make(self._group, format_type=FormatType.FLOAT32)
        self._out_params = OutputVStreamParams.make(self._group, format_type=FormatType.FLOAT32)
        self._infer_streams = InferVStreams
        if int(np.prod(self._input.shape)) != self.mean.size:
            raise ValueError(f"Model input {self._input.shape} does not match {self.mean.size} feature bins")

    def reconstruction_error(self, features):
        """Mean squared reconstruction error for each row of log-power features."""
        normalised = ((np.asarray(features, dtype=np.float32) - self.mean) / self.std).astype(np.float32)
        batch = normalised.reshape((normalised.shape[0],) + tuple(self._input.shape))
        with self._infer_streams(self._group, self._in_params, self._out_params) as pipeline:
            with self._group.activate(self._group_params):
                result = pipeline.infer({self._input.name: batch})[self._output.name]
        rebuilt = np.asarray(result, dtype=np.float32).reshape(normalised.shape)
        return np.mean((rebuilt - normalised) ** 2, axis=1)
