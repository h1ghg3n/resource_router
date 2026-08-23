FROM python:3.10-slim

ARG JETSON_STATS_VERSION=4.3.2

WORKDIR /app

COPY pyproject.toml README.md LICENSE.md ./
COPY app ./app

RUN python -m pip install --no-cache-dir \
    . \
    "jetson-stats==${JETSON_STATS_VERSION}"

RUN mkdir -p /app/data

ENV JETROUTER_HOST=0.0.0.0 \
    JETROUTER_PORT=19081 \
    JETROUTER_DATABASE_PATH=/app/data/jetrouter.sqlite3

EXPOSE 19081

CMD ["python", "-m", "app"]
