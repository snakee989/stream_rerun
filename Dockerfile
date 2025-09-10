# Use Debian 12 slim as the base image for compatibility with NVIDIA and Intel drivers
FROM debian:12-slim

# Set environment variables for NVIDIA GPU support
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# Set non-interactive frontend to avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies in a single layer to reduce image size
# Includes Python, FFmpeg, Intel VA-API drivers, NVIDIA encoding libraries, and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3=3.11.2-6 \
        python3-pip=23.0.1+dfsg-1 \
        ffmpeg=7:5.1.6-0+deb12u1 \
        libva-drm2=2.14.0-1 \
        libva-x11-2=2.14.0-1 \
        libva-dev=2.14.0-1 \
        intel-media-va-driver-non-free=23.1.1+dfsg1-1 \
        vainfo=2.14.0-1 \
        libnvidia-encode1=525.147.05-7~deb12u1 \
        build-essential=12.9 \
        curl=7.88.1-10+deb12u7 \
        ca-certificates=20230311 \
        git=1:2.39.2-1.1 \
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
      version="1.0"
