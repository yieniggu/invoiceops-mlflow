# Build:  docker build -t invoiceops-mlflow-gateway gateway
# Dependencies come from uv.lock (`uv sync --frozen`: any drift between pyproject.toml and the
# lockfile fails the build, and every wheel is verified against the hashes recorded there).
# Base images are pinned by tag AND digest of the multi-arch index (amd64/arm64); refresh the
# digests deliberately with `docker buildx imagetools inspect <image:tag>`.
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS builder
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 --no-create-home gateway
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/_gateway/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "invoiceops_mlflow_gateway"]
