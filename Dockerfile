FROM python:3.10-slim

ARG JETSON_STATS_VERSION=7.2.2
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/h1ghg3n/resource_router" \
    org.opencontainers.image.revision="${VCS_REF}"

WORKDIR /app

COPY pyproject.toml README.md LICENSE.md ./
COPY app ./app

RUN python -m pip install --no-cache-dir \
    . \
    "jetson-stats==${JETSON_STATS_VERSION}"

# Source checkout modes must not prevent the configured non-root UID from
# importing the application. This affects image files, not host permissions.
RUN chmod -R a+rX /app/app && mkdir -p /app/data

ENV JETROUTER_HOST=0.0.0.0 \
    JETROUTER_PORT=19081 \
    JETROUTER_DATABASE_PATH=/app/data/jetrouter.sqlite3

EXPOSE 19081

CMD ["python", "-m", "app"]
