FROM python:3.11-slim

LABEL maintainer="bybit-paper-harness"
LABEL description="Multi-agent paper trading infrastructure for Bybit"

WORKDIR /app

# Install dependencies first (leverages layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ ./src/
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

# Runtime directories
RUN mkdir -p logs reports configs/cache

# Run as unprivileged user
RUN useradd -m -u 1000 harness && \
    chown -R harness:harness /app
USER harness

# Default command (override with --config on docker run)
ENTRYPOINT ["python3", "-m", "src.harness"]
CMD ["--config", "configs/example_doge.json"]
