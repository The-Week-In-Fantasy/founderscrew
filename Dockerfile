# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --user .

# Stage 2: Final runtime image
FROM python:3.12-slim

WORKDIR /app

# Install git for repository cloning/pulling and curl for diagnostics
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed dependencies from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy package source code
COPY founderscrew/ founderscrew/
COPY README.md pyproject.toml ./

# Install editable package for entrypoint linking
RUN pip install -e .

# Environment variables for Cloud Run
ENV PORT=8080
ENV FOUNDERSCREW_STORAGE_BACKEND=firestore
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Run uvicorn server in headless mode
CMD ["uvicorn", "founderscrew.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
