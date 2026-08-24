# ==============================================================================
# Production Dockerfile for Podcast Shorts Generator
# ==============================================================================
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=5000
ENV HOST=0.0.0.0

# Install FFmpeg, OpenCV runtime dependencies, and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies first for caching (suppress root user warning)
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Copy project files
COPY . .

# Ensure storage directories exist
RUN mkdir -p input output temp logs data frontend

# Expose server port
EXPOSE 5000

# Health check (supports FastAPI /health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:${PORT:-5000}/health || exit 1

# Start the unified web server & API (FastAPI + Uvicorn, fast, supports --reload)
# Use shell form to allow $PORT expansion; uvicorn handles $PORT correctly
CMD python server.py --host 0.0.0.0 --port ${PORT:-5000}
