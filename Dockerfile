# Use a base image with a recent version of Ubuntu
FROM ubuntu:22.04

# Set environment variables to enable non-interactive apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Install core build dependencies
RUN apt-get update && apt-get install -y \
    software-properties-common \
    git \
    build-essential \
    cmake \
    libtool \
    nasm \
    yasm \
    libx264-dev \
    libx265-dev \
    libvpx-dev \
    ca-certificates \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# --- Intel oneAPI (libvpl) Setup ---
# Add Intel's new oneAPI apt repository and key
RUN curl -fsSL https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB | gpg --dearmor | tee /usr/share/keyrings/oneapi-archive-keyring.gpg > /dev/null && \
    echo "deb [signed-by=/usr/share/keyrings/oneapi-archive-keyring.gpg] https://apt.repos.intel.com/oneapi all main" | tee /etc/apt/sources.list.d/oneAPI.list

# Update and install Intel Media SDK libraries required for oneVPL
RUN apt-get update && apt-get install -y \
    libmfx-dev \
    intel-media-va-driver-non-free \
    libvpl-dev \
    libmfx1 \
    && rm -rf /var/lib/apt/lists/*

# --- NVIDIA NVENC Setup ---
# Add NVIDIA package repository
RUN curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/3bf863cc.pub | gpg --dearmor -o /usr/share/keyrings/nvidia-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nvidia-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /" | tee /etc/apt/sources.list.d/nvidia.list

# Install the full CUDA toolkit and encoding libraries
RUN apt-get update && apt-get install -y \
    libnvidia-encode-535 \
    cuda-toolkit-12-2 \
    && rm -rf /var/lib/apt/lists/*

# --- Compile FFmpeg with both Intel oneVPL and NVIDIA NVENC support ---
WORKDIR /usr/src
RUN git clone https://github.com/FFmpeg/FFmpeg.git -b release/4.4 --depth 1
WORKDIR /usr/src/FFmpeg

# Configure and compile FFmpeg with all necessary hardware acceleration flags
RUN ./configure \
    --prefix=/usr/local \
    --enable-shared \
    --enable-gpl \
    --enable-libx264 \
    --enable-libx265 \
    --enable-libvpx \
    --enable-nonfree \
    --enable-libmfx \
    --enable-libvpl \
    --enable-cuda \
    --enable-cuvid \
    --enable-nvenc \
    --extra-libs=-lpthread \
    --extra-cflags="-I/usr/include/mfx" \
    --extra-ldflags="-L/usr/lib/x86_64-linux-gnu -lmfx"

RUN make -j$(nproc)
RUN make install

# Your application-specific instructions go here
# Install Python and application dependencies
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN pip3 install flask gunicorn

WORKDIR /app
COPY . /app

# Expose the port your application will listen on
EXPOSE 5000

# The command to run your application using gunicorn for a production-ready server
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
