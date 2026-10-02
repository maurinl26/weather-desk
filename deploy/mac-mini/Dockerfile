FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl libeccodes0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 weather \
    && useradd --uid 10001 --gid weather --create-home --shell /usr/sbin/nologin weather

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
RUN uv run playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright
COPY app.py ./app.py
COPY weather_desk ./weather_desk
COPY deploy/mac-mini/container-entrypoint.sh /usr/local/bin/weather-desk-entrypoint
RUN chmod 755 /usr/local/bin/weather-desk-entrypoint \
    && mkdir -p /app/data \
    && chown weather:weather /app/data

USER weather:weather
EXPOSE 5006
ENTRYPOINT ["/usr/local/bin/weather-desk-entrypoint"]
CMD ["panel", "serve", "app.py", "--address", "0.0.0.0", "--port", "5006", "--allow-websocket-origin", "weather-desk.galerne-routing.com", "--use-xheaders", "--liveness"]
