FROM python:3.12-slim
RUN useradd -r -u 10003 -m support
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations
USER support
EXPOSE 9500
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9500/healthz',timeout=2).status==200 else 1)"
# One worker: open chat sockets and rate limits live in this process (app/hub.py). The client address
# comes from Caddy's X-Forwarded-For, read by app/http.py, so uvicorn's own proxy handling is off.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "9500", "--workers", "1", "--no-proxy-headers", "--no-server-header", "--ws-max-size", "65536", "--timeout-graceful-shutdown", "10"]
