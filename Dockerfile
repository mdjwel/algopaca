# syntax=docker/dockerfile:1
FROM python:3.11-slim

LABEL maintainer="AlgoPaca Contributors"
LABEL description="AlgoPaca - Algorithmic Paper & Live Trading Desk for Alpaca"

# Environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALGOPACA_HOST=0.0.0.0 \
    ALGOPACA_PORT=8765

WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY bot/ ./bot/
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY README.md LICENSE ./

# Create data directory and non-root user
RUN mkdir -p /app/data && \
    useradd -m -u 1000 algopaca && \
    chown -R algopaca:algopaca /app

USER algopaca

EXPOSE 8765

# Container healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8765/ || exit 1

CMD ["python", "-m", "bot.webapp"]
