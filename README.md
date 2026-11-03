# Scalable E-Commerce Platform

FastAPI e-commerce backend, work in progress, with:

- JWT auth with refresh tokens and RBAC
- PostgreSQL with async SQLAlchemy 2.0 and Alembic migrations
- Redis caching and webhook idempotency
- Razorpay and Stripe payment scaffolding
- RabbitMQ-backed async notifications
- Prometheus metrics, JSON logging, and Nginx edge config
- GitHub Actions pipeline: CI runs the tests against Postgres and Redis (not RabbitMQ) and builds the Docker image as a build check; the image is not pushed anywhere. On pushes to main (and manual runs from main), when the three AWS secrets exist, an SSH step checks out the tested commit on the host with git and runs `docker compose -f docker-compose.prod.yml up -d --build`. The host needs a git clone, `.env.prod` and certificates first. This deploy step has not been run on a real host yet.

## Project Structure

```text
app/        FastAPI source code
tests/      Pytest suite
alembic/    Database migrations
nginx/      Reverse proxy and static asset config
```

## Tech Stack

- FastAPI
- PostgreSQL 15
- SQLAlchemy 2.0 async
- Alembic
- Redis
- RabbitMQ
- Razorpay / Stripe
- Prometheus
- Nginx
- Docker Compose
- GitHub Actions

## Quick Start

### 1. Configure environment

Copy `.env.example` values into `.env` and update secrets as needed.

Important variables:

- `JWT_SECRET_KEY`
- `JWT_REFRESH_SECRET_KEY`
- `REDIS_URL`
- `RABBITMQ_URL`
- `RAZORPAY_KEY_ID`
- `RAZORPAY_KEY_SECRET`
- `RAZORPAY_WEBHOOK_SECRET`
- `STRIPE_SECRET_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET`

### 2. Run with Docker Compose

```bash
docker compose up --build
```

App endpoints:

- API: `http://localhost:8000`
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- RabbitMQ UI: `http://localhost:15672`

### 3. Run Alembic migrations

If you are running migrations manually inside the app container:

```bash
alembic upgrade head
```

## Local Development

Install dependencies:

```bash
pip install -e ".[dev]"
```

Run the API:

```bash
uvicorn app.main:app --reload
```

Run the worker:

```bash
python -m app.worker
```

## Testing

Run the full suite:

```bash
pytest
```

Coverage is enforced through `pyproject.toml` with:

- `--cov=app`
- `--cov-report=term-missing`
- `--cov-fail-under=71`

The coverage gate is 71% and will be raised as tests are added. The last full run measured about 73%; the modules below 60% were `app/worker.py` (0%), `app/api/routes/products.py` (31%), `app/core/messaging.py` (53%), `app/core/cache.py` (58%) and `app/services/payments.py` (59%).

## Core API Areas

### Auth

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `GET /api/v1/auth/google/login` (planned, not finished)
- `POST /api/v1/auth/google/callback` (planned, not finished)
- `GET /api/v1/auth/me`

### Products

- `GET /api/v1/products`
- `GET /api/v1/products/{id}`
- `GET /api/v1/products/search`
- `POST /api/v1/products`
- `PUT /api/v1/products/{id}`
- `DELETE /api/v1/products/{id}`

Admin-only product mutations are protected by RBAC.

### Payments

- `POST /api/v1/orders`
- `POST /api/v1/payments/webhooks/{provider}`

Orders are marked `PAID` only after verified webhook processing.

## Caching and Messaging

### Redis

Used for:

- product list/detail caching
- cache invalidation on admin product writes
- webhook idempotency protection

### RabbitMQ

Used for:

- publishing `order paid` notification messages
- consuming notification events in a background worker
- logging email-send placeholders asynchronously

## Observability

### Metrics

Prometheus-compatible metrics are exposed at:

```text
/metrics
```

### Logging

Application logs are JSON formatted and ready for ELK-style ingestion.

### Nginx

The reverse proxy config is in [nginx/nginx.conf](nginx/nginx.conf).

To enable the edge profile:

```bash
docker compose --profile edge up --build
```

Before enabling HTTPS, place certificates at:

- `nginx/certs/fullchain.pem`
- `nginx/certs/privkey.pem`

## CI/CD

GitHub Actions workflow:

- [deploy.yml](.github/workflows/deploy.yml)

Pipeline stages:

- install dependencies
- run pytest with coverage gate
- build Docker image (a build check; the image is not pushed)
- on main, optionally update an EC2 host over SSH: check out the tested commit, then run `docker compose up -d --build`

### Required GitHub Secrets

Only the EC2 deploy step uses secrets. Without all three, the step is skipped:

- `AWS_EC2_HOST`
- `AWS_EC2_USERNAME`
- `AWS_EC2_SSH_KEY`

## Deployment Notes

The production stack is [docker-compose.prod.yml](docker-compose.prod.yml). Its settings go in `.env.prod`: copy [.env.prod.example](.env.prod.example) and use URL-safe secrets (for example `openssl rand -hex 24`).

- nginx is the only service that publishes ports (80 and 443). Postgres, Redis and RabbitMQ are on an internal Docker network.
- A one-shot `migrate` service runs `alembic upgrade head` before the app and worker start.
- migrate runs on every deploy (alembic upgrade head) and applies nothing when there are no new migrations. A migration runs while the old app is still serving, so it must work with the old code.
- The app refuses to start with `APP_ENV=production` while a secret is empty or still a default.
- `/metrics` returns 403 through nginx. There is no Prometheus in the compose file; it would have to scrape `app:8000/metrics` inside the Docker network.
- App logs are JSON on stdout. The compose file sets a Docker log limit of 3 files of 10 MB on every service except migrate (set in the file, not checked in a running container).

Before the first deploy, the host needs:

- Docker Engine with the Compose plugin, and git
- a full clone of this repository at `/opt/scalable-ecommerce-platform`, with `origin` set, owned by (or writable by) the SSH user; a read-only deploy key if the repository is private
- `.env.prod`, filled in
- `fullchain.pem` and `privkey.pem` in `nginx/certs/`
- the SSH user in the `docker` group, and ports 22, 80 and 443 reachable

The deploy step stops if tracked files on the host have local changes, checks out the exact commit that passed the tests, checks that the host is on that commit, then runs `docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build`. It builds the image on the host.

It does not roll back, and it is not a zero-downtime deploy: the app restarts. None of it has been run on a real host yet.

## Next Improvements

- real email provider integration
- inventory reservation and release flow
- order history endpoints
- background retry / dead-letter queue strategy
- Terraform or ECS deployment automation
