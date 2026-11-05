FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock
COPY pyproject.toml README.md LICENSE uv.lock /app/
COPY src /app/src
RUN pip install --no-cache-dir --no-deps /app
USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=10s --timeout=4s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health/ready', timeout=3)"]
CMD ["uvicorn", "chronicle.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8787", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "32", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "30"]
