# Use Debian 12 slim as the base image for compatibility with NVIDIA and Intel drivers
FROM debian:12-slim

# Set environment variables for NVIDIA GPU support
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# Set non-interactive frontend to avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Add non-free and contrib repositories, and NVIDIA CUDA repository
RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free" > /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian-security bookworm-security main contrib non-free" >> /etc/apt/sources.list && \
    echo "deb [arch=amd64] https://download.nvidia.com/debian bookworm non-free" >> /etc/apt/sources.list.d/nvidia.list && \
    curl -fsSL https://download.nvidia.com/debian/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nvidia-archive-keyring.gpg] https://download.nvidia.com/debian bookworm non-free" >> /etc/apt/sources.list.d/nvidia.list

# Install dependencies in a single layer
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
        curl \
        ca-certificates \
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
      version="1.1"
