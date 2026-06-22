# Mock GitLab receiver for ach-agent e2e testing.
# DEV TOOLING ONLY — never used in production.
#
# Multi-stage build per CLAUDE.md: builder installs deps into /install via
# --prefix so both site-packages AND console scripts (bin/) are preserved.
# Runtime copies /install → /usr/local so python -m uvicorn works.
# Explicit COPY paths only (per CLAUDE.md).

FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
# Use --prefix=/install so the full layout (lib/python3.x/site-packages
# AND bin/) is preserved and can be copied atomically to /usr/local.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

WORKDIR /app

# Copy installed dependencies from builder stage (preserves bin/ + site-packages)
COPY --from=builder /install /usr/local

# Copy only the application file (explicit path)
COPY app.py .

EXPOSE 9080

# Use module form so no console-script launcher is required.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9080"]
