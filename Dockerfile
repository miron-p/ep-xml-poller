FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data BIND_HOST=0.0.0.0 PORT=8000
WORKDIR /app
RUN groupadd --gid 10001 poller && useradd --uid 10001 --gid poller --no-create-home poller && mkdir /data && chown poller:poller /data
COPY poller.py /app/poller.py
USER poller
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).read()"
CMD ["python", "poller.py", "serve"]
