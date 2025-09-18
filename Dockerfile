# Use a more compatible base image
FROM ubuntu:22.04

# Non-interactive and TZ
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System deps (incl. wget for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates gnupg wget \
    && rm -rf /var/lib/apt/lists/*

# Add FFmpeg repo (Jellyfin) and install FFmpeg + VAAPI tools
RUN wget -O - https://repo.jellyfin.org/ubuntu/jellyfin_team.gpg.key | apt-key add - \
    && echo "deb [arch=amd64] https://repo.jellyfin.org/ubuntu jammy main" > /etc/apt/sources.list.d/jellyfin.list
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libva-drm2 libva2 vainfo \
    && rm -rf /var/lib/apt/lists/*

# Optional NVIDIA runtime libraries (best-effort)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnvidia-encode-525 libnvidia-decode-525 \
    && rm -rf /var/lib/apt/lists/* || echo "NVIDIA libraries not available, continuing"

# Optional Intel media driver (best-effort)
RUN apt-get update && apt-get install -y --no-install-recommends \
    intel-media-va-driver-non-free \
    && rm -rf /var/lib/apt/lists/* || echo "Intel media driver not available, continuing"

# Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Requirements (ensure gunicorn and redis are listed)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Create app dirs
RUN mkdir -p videos logs

# Non-root user
RUN useradd -m -u 1000 streamer && chown -R streamer:streamer /app
USER streamer

# Expose app port
EXPOSE 5000

# Health check (avoid Python 'requests' dependency)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:5000/health || exit 1

# Start with Gunicorn (threads for light concurrency; infinite timeout for long ops)
# Adjust workers/threads for your host. Example: 2 workers, 4 threads each.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
