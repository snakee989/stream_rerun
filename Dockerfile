FROM ubuntu:24.04

# Non-interactive and TZ
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install everything in one RUN layer for better caching
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget \
    ffmpeg \
    libva-drm2 libva2 vainfo intel-media-va-driver-non-free \
    libnvidia-encode-550 libnvidia-decode-550 \
    intel-gpu-tools libmfx1 libmfx-tools \
    libx264-dev libx265-dev libvpx-dev libfdk-aac-dev \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Verify FFmpeg
RUN ffmpeg -version | head -n1

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

RUN mkdir -p videos logs

# Simply use UID 1000 (the default ubuntu user)
USER 1000

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:5000/health || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
