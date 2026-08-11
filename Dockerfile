FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system gateway \
    && useradd --system --gid gateway --home-dir /app gateway

COPY --chown=gateway:gateway pyproject.toml README.md alembic.ini ./
COPY --chown=gateway:gateway app ./app
COPY --chown=gateway:gateway loadtest ./loadtest
COPY --chown=gateway:gateway migrations ./migrations
COPY --chown=gateway:gateway scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

RUN pip install . \
    && chmod 755 ./scripts/docker-entrypoint.sh

USER gateway

EXPOSE 8000

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
