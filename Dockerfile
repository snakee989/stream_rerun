# Use a more compatible base image
FROM ubuntu:22.04

# Set environment variables to avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gnupg \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Add FFmpeg repository for latest version
RUN wget -O - https://repo.jellyfin.org/ubuntu/jellyfin_team.gpg.key | apt-key add - \
    && echo "deb [arch=amd64] https://repo.jellyfin.org/ubuntu jammy main" > /etc/apt/sources.list.d/jellyfin.list

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libva-drm2 \
    libva2 \
    vainfo \
    && rm -rf /var/lib/apt/lists/*

# Install NVIDIA drivers if available (will skip if not present)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnvidia-encode-525 \
    libnvidia-decode-525 \
    && rm -rf /var/lib/apt/lists/* || \
    echo "NVIDIA libraries not available, continuing without them"

# Install Intel media driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    intel-media-va-driver-non-free \
    && rm -rf /var/lib/apt/lists/* || \
    echo "Intel media driver not available, continuing without it"

# Install Python and pip
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories
RUN mkdir -p videos logs

# Create a non-root user
RUN useradd -m -u 1000 streamer && \
    chown -R streamer:streamer /app

# Switch to non-root user
USER streamer

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import requests; requests.get('http://localhost:5000/health', timeout=5)"

# Run application
CMD ["python3", "app.py"]
