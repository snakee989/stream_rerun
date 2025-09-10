# Use Debian 12 (Bookworm) as the base image for compatibility with NVIDIA and Intel drivers
FROM debian:12

# Set environment variables for NVIDIA GPU support
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# Set non-interactive frontend to avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Add non-free, contrib, non-free-firmware, and NVIDIA repositories, and install prerequisites
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        ca-certificates && \
    echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    curl -fsSL https://nvidia.github.io/nvidia-container-toolkit/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/nvidia-container-toolkit/debian12/amd64/ /" > /etc/apt/sources.list.d/nvidia-container-toolkit.list && \
    apt-get update && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Install dependencies for NVIDIA and Intel iGPU support
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
        libva-drm2 \
        libva-x11-2 \
        libva-dev \
        intel-media-va-driver-non-free \
        vainfo \
        libnvidia-encode1 \
        build-essential \
        git \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Link python3 to python for convenience
RUN ln -sf /usr/bin/python3 /usr/bin/python

# Set working directory
WORKDIR /app

# Create videos directory
RUN mkdir -p /app/videos

# Copy application files
COPY app.py requirements.txt /app/

# Install Python dependencies and clean up pip cache
RUN pip install --no-cache-dir -r requirements.txt && \
    rm -rf /root/.cache/pip

# Expose port for the application
EXPOSE 8080

# Add health check to verify application is running
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Define volume for video storage
VOLUME /app/videos

# Run the application
CMD ["python", "app.py"]

# Metadata
LABEL maintainer="Your Name <your.email@example.com>" \
      description="Docker image for video processing with NVIDIA GPU (NVENC) and Intel iGPU (VA-API) support" \
      version="1.5"
