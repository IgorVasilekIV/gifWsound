# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /usr/local/bin/

WORKDIR /app

# Сначала манифесты, чтобы слой зависимостей кэшировался.
COPY pyproject.toml uv.lock ./
COPY src ./src

# uv sync собирает src/gifwsound через uv_build backend и ставит в .venv.
RUN uv sync --frozen --no-dev

# Конфигурация и прочее — мелкий слой.
COPY docker-entrypoint.sh ./
RUN chmod +x ./docker-entrypoint.sh

# /app/logs — для хост-логов (RW volume). /tmp — tmpfs (256m) для временных видео.
RUN mkdir -p /app/logs

USER 1000:1000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; \
urllib.request.urlopen('https://api.telegram.org/bot'+os.environ['BOT_TOKEN']+'/getMe', timeout=5)"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gifwsound"]