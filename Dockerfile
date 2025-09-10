# =========================================================================
# Stage 1: Build Stage
# This stage installs build tools and Python dependencies in a virtual env.
# =========================================================================
FROM debian:12-slim AS builder

# Set non-interactive frontend to avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3-pip \
        python3-venv \
        git \
        build-essential && \
    rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python3 -m venv /opt/venv

# Activate the virtual environment for subsequent commands
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install Python dependencies, leveraging build cache
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# =========================================================================
# Stage 2: Final Stage
# This stage builds the final, lean image with only runtime dependencies.
# =========================================================================
FROM debian:12-slim

# Set environment variables for NVIDIA GPU support at runtime
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# Set non-interactive frontend
ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies in a single layer for efficiency
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        # Repo management
        curl \
        gnupg \
        ca-certificates && \
    # --- FIX STARTS HERE ---
    # Create the sources.list file directly since debian:slim doesn't have it by default
    echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    # --- FIX ENDS HERE ---
    # Add NVIDIA container toolkit repository
    curl -fsSL https://nvidia.github.io/nvidia-container-toolkit/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/nvidia-container-toolkit/debian12/amd64/ /" > /etc/apt/sources.list.d/nvidia-container-toolkit.list && \
    # Update sources and install runtime dependencies
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        ffmpeg \
        # Intel VA-API dependencies
        libva-drm2 \
        libva-x11-2 \
        libva-dev \
        intel-media-va-driver-non-free \
        vainfo \
        # NVIDIA NVENC dependency
        libnvidia-encode1 && \
    # Clean up to reduce image size
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user and group for better security
RUN groupadd --system --gid 1001 appgroup && \
    useradd --system --uid 1001 --gid 1001 appuser

# Set working directory
WORKDIR /app

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application code and set ownership
COPY --chown=appuser:appgroup app.py .

# Activate the virtual environment for the final container
ENV PATH="/opt/venv/bin:$PATH"

# Switch to the non-root user
USER appuser

# Expose port for the application
EXPOSE 8080

# Add health check to verify the application is running
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Define volume for video storage. This directory will be created automatically.
VOLUME /app/videos

# Metadata
LABEL maintainer="Your Name <your.email@example.com>" \
      description="Docker image for video processing with NVIDIA GPU (NVENC) and Intel iGPU (VA-API) support" \
      version="1.6"

# Run the application
CMD ["python", "app.py"]
