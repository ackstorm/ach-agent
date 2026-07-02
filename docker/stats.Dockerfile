# syntax=docker/dockerfile:1
# Stage 1 — build the SPA
FROM node:22-slim AS ui
WORKDIR /ui
COPY src/ach_stats/ui/package.json src/ach_stats/ui/package-lock.json ./
RUN npm ci
COPY src/ach_stats/ui/ ./
RUN npm run build

# Stage 2 — python deps (uv, matching the harness image's convention). Installed by
# exact name/pin (not `uv pip install .`) so this stage needs only pyproject.toml,
# not the app/ source — the runtime stage below copies app/ as plain files.
FROM python:3.12-slim AS deps
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
RUN uv pip install --system --no-cache-dir --target=/app/site-packages \
    "fastapi==0.116.1" "uvicorn[standard]==0.34.0" "redis>=5,<6" "pydantic>=2,<3" \
 && find /app/site-packages -type d -name "__pycache__" -prune -exec rm -rf {} +

# Stage 3 — runtime
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/site-packages
COPY --from=deps /app/site-packages /app/site-packages
COPY src/ach_stats/api/app /app/app
COPY --from=ui /ui/dist /app/ui/dist
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
