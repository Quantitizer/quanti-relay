# Quanti Relay: one container, paper trading until its owner says otherwise.
#   docker compose up        (see compose.yaml and the README's quickstart)
FROM python:3.11.11-slim-bookworm@sha256:081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5

COPY --from=ghcr.io/astral-sh/uv:0.5.11@sha256:0ac957607303916420297a4c9c213bb33fbd3c888f9cd7f4f7273596ebf42b85 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE alembic.ini ./
COPY relay ./relay
COPY schemas ./schemas
RUN uv sync --frozen --no-dev --extra venues --extra postgres \
    && useradd --system --uid 10001 --home-dir /app relay \
    && mkdir -p /data /config \
    && chown relay /data

ENV PATH="/app/.venv/bin:$PATH" \
    RELAY_DATABASE_URL="sqlite+aiosqlite:////data/relay.db" \
    RELAY_CONFIG_DIR="/config" \
    RELAY_DRY_RUN="true"
USER relay
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
# Outside localhost the relay refuses to start without RELAY_API_KEY (every endpoint would be open).
CMD ["relay", "serve", "--host", "0.0.0.0", "--port", "8080"]
