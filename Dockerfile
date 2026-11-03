FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY pyproject.toml uv.lock /code/
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

ENV PATH="/code/.venv/bin:$PATH"

COPY --chown=10001:10001 . /code

USER 10001:10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
