FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git fonts-dejavu-core wkhtmltopdf \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --create-home app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir langsmith

COPY pyproject.toml README.md ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY scripts ./scripts
COPY skills ./skills

RUN test -f /app/src/search_agent/main.py
RUN if [ ! -f /app/src/search_agent/__init__.py ]; then \
      printf '"""Search agent package."""\n' > /app/src/search_agent/__init__.py; \
    fi

RUN pip install --no-cache-dir . \
    && python -c "import search_agent; import search_agent.main" \
    && chmod +x /app/scripts/container_entrypoint.sh \
    && chown -R app:app /app
USER app

ENTRYPOINT ["/app/scripts/container_entrypoint.sh"]

CMD ["python", "-m", "search_agent.main"]
