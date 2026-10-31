# Scalable E-Commerce Platform

FastAPI e-commerce backend, work in progress, with:

- JWT auth with refresh tokens and RBAC
- PostgreSQL with async SQLAlchemy 2.0 and Alembic migrations
- Redis caching and webhook idempotency
- Razorpay and Stripe payment scaffolding
- RabbitMQ-backed async notifications
- Prometheus metrics, JSON logging, and Nginx edge config
- GitHub Actions CI/CD with Docker build and optional EC2 deploy

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

Measured coverage is about 71%. The gate is set to that real number and will be raised as tests are added. Product routes, `dependencies.py` and the worker are the least covered.

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
- build Docker image
- optionally push to Docker Hub
- optionally deploy to AWS EC2 via SSH

### Required GitHub Secrets

For image push:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

For EC2 deploy:

- `AWS_EC2_HOST`
- `AWS_EC2_USERNAME`
- `AWS_EC2_SSH_KEY`

## Deployment Notes

- Use the `nginx` edge profile in production.
- Run Alembic migrations during deployment before accepting traffic.
- Store provider secrets in environment variables or a secret manager.
- Keep Redis and RabbitMQ on private network segments in AWS.
- Point Prometheus to `/metrics` and ship JSON logs to your logging stack.

## Next Improvements

- real email provider integration
- inventory reservation and release flow
- order history endpoints
- background retry / dead-letter queue strategy
- Terraform or ECS deployment automation
