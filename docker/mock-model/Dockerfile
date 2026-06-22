# Mock OpenAI-compatible model server for ach-agent round-trip testing.
# DEV TOOLING ONLY — never used in production.
#
# Multi-stage build per CLAUDE.md: builder installs deps into /install via
# --prefix so both site-packages AND console scripts (bin/) are preserved.
# Runtime copies /install → /usr/local so python -m uvicorn works.
# No COPY . .

FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
# CR-02: use --prefix=/install so the full layout (lib/python3.x/site-packages
# AND bin/) is preserved and can be copied atomically to /usr/local.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

WORKDIR /app

# Copy installed dependencies from builder stage (preserves bin/ + site-packages)
COPY --from=builder /install /usr/local

# Copy only the application file (explicit path — no COPY . .)
COPY app.py .

EXPOSE 8080

# CR-02: use module form so no console-script launcher is required.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
