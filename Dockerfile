FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/dineshpalsingh1989/VDS-to-Dashboard" \
    org.opencontainers.image.title="VDS-to-Dashboard" \
    org.opencontainers.image.description="MQTT vibration monitoring, SQLite history, and advisory diagnostics" \
    org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    VDS_HOST=0.0.0.0 VDS_PORT=8050 VDS_CONFIG_DIR=/data/config VDS_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home vds \
    && mkdir -p /data/config && chown -R vds:vds /data
COPY --chown=vds:vds app.py ./
COPY --chown=vds:vds vds_dashboard ./vds_dashboard
COPY --chown=vds:vds assets ./assets
USER vds
EXPOSE 8050
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8050/healthz', timeout=3)"
CMD ["python", "app.py"]
