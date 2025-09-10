# Use a base image with a recent version of Ubuntu
FROM ubuntu:22.04

# Set environment variables to enable non-interactive apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Install core dependencies and FFmpeg
RUN apt-get update && apt-get install -y \
    software-properties-common \
    ffmpeg \
    python3 \
    python3-pip \
    ca-certificates \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# --- Intel Quick Sync Video (QSV) Setup ---
# Add Intel's new oneAPI apt repository and key
RUN curl -fsSL https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB | gpg --dearmor | tee /usr/share/keyrings/oneapi-archive-keyring.gpg > /dev/null && \
    echo "deb [signed-by=/usr/share/keyrings/oneapi-archive-keyring.gpg] https://apt.repos.intel.com/oneapi all main" | tee /etc/apt/sources.list.d/oneAPI.list

# Update apt-get and install Intel Media SDK and libraries required for QSV
# The package names have changed from the older SDK to newer components.
RUN apt-get update && apt-get install -y \
    libmfx-dev \
    intel-media-va-driver-non-free \
    libmfx1 \
    && rm -rf /var/lib/apt/lists/*

# --- NVIDIA NVENC Setup ---
# Add NVIDIA package repository
RUN curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/3bf863cc.pub | gpg --dearmor -o /usr/share/keyrings/nvidia-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nvidia-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /" | tee /etc/apt/sources.list.d/nvidia.list

# Install only the NVIDIA encoding libraries without the full CUDA toolkit
RUN apt-get update && apt-get install -y \
    libnvidia-extra-535 \
    && rm -rf /var/lib/apt/lists/*

# Your application-specific instructions go here
# For example, to copy your Python application
WORKDIR /app
COPY . /app

# The command to run your application
CMD ["python3", "app.py"]
