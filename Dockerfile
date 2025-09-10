# Use slim Python base
FROM python:3.12-slim

# Enable NVIDIA runtime if using GPU
ENV NVIDIA_VISIBLE_DEVICES all
ENV NVIDIA_DRIVER_CAPABILITIES compute,video,utility

# Install system dependencies for FFmpeg + VA-API + NVENC
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libva2 \
        vainfo \
        libnvidia-encode1 \
        build-essential \
        curl \
        ca-certificates \
        git \
        && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy app files
COPY app.py /app/

# Copy videos folder (optional, user can mount host folder)
RUN mkdir -p /app/videos

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 8080

# Run the app
CMD ["python", "app.py"]
