# Use Debian 12 slim
FROM debian:12-slim

# Set environment variables for NVIDIA GPU
ENV NVIDIA_VISIBLE_DEVICES all
ENV NVIDIA_DRIVER_CAPABILITIES compute,video,utility

# Install basic dependencies + FFmpeg + VA-API + NVENC
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
        vainfo \
        libva2 \
        libnvidia-encode1 \
        build-essential \
        curl \
        ca-certificates \
        git \
        && rm -rf /var/lib/apt/lists/*

# Set Python command
RUN ln -s /usr/bin/python3 /usr/bin/python

# Create app directory
WORKDIR /app

# Copy app files
COPY app.py /app/
COPY requirements.txt /app/

# Create videos folder
RUN mkdir -p /app/videos

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port
EXPOSE 8080

# Run the app
CMD ["python", "app.py"]
