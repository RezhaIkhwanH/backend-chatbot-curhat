# =========================
# Stage 1: Builder
# =========================
FROM python:3.12-slim-bookworm AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install build deps (untuk bcrypt dll)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency file dulu
COPY pyproject.toml uv.lock ./

# Install dependencies ke .venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# Copy source code (SETELAH install)
COPY . .

# =========================
# Stage 2: Runtime
# =========================
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy hasil build
COPY --from=builder /app /app

# Pakai venv
ENV PATH="/app/.venv/bin:$PATH"

# Best practice Python
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port FastAPI
EXPOSE 8000

# Jalankan pakai uvicorn (lebih benar untuk FastAPI)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]