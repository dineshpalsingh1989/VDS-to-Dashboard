# Builds the Hailo HEF files for the neural model on any x86-64 Linux PC with Docker.
# Hailo's Dataflow Compiler is not public: download its wheel (hailo_dataflow_compiler-*.whl)
# from the Hailo Developer Zone and put it in iriv-edge/hailo/dfc/. Then: bash iriv-edge/hailo/build_hef.sh
FROM python:3.10-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential graphviz graphviz-dev \
 && rm -rf /var/lib/apt/lists/*
COPY iriv-edge/hailo/dfc/ /dfc/
RUN pip install --no-cache-dir /dfc/*.whl numpy onnx
WORKDIR /repo
ENTRYPOINT ["python", "iriv-edge/hailo/compile_hef.py"]
