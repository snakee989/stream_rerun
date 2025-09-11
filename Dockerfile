# CUDA dev base on Ubuntu 22.04
FROM nvidia/cuda:12.2.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
# Prefer modern Intel VAAPI driver inside the container
ENV LIBVA_DRIVER_NAME=iHD
ENV LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri

# Core build deps + codec/dev libs for FFmpeg and VAAPI
RUN apt-get update && apt-get install -y \
    software-properties-common \
    git \
    build-essential \
    cmake \
    libtool \
    nasm \
    yasm \
    pkg-config \
    ca-certificates \
    curl \
    gnupg \
    wget \
    gpg-agent \
    libx264-dev \
    libx265-dev \
    libvpx-dev \
    libva-dev \
    libdrm-dev \
    && rm -rf /var/lib/apt/lists/*

# Add Intel Graphics (GPU) repo for jammy and install oneVPL + VAAPI runtime
RUN set -eux; \
    wget -qO - https://repositories.intel.com/gpu/intel-graphics.key | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg; \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu jammy unified" \
      > /etc/apt/sources.list.d/intel-gpu-jammy.list; \
    apt-get update; \
    apt-get install -y \
      libvpl-dev \
      onevpl-intel-gpu \
      intel-media-va-driver-non-free \
      libva2 \
      vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

# Install NVIDIA Video Codec headers (required for --enable-nvenc)
RUN git clone https://github.com/FFmpeg/nv-codec-headers.git --depth 1 && \
    cd nv-codec-headers && make install && cd .. && rm -rf nv-codec-headers

# Get FFmpeg source
RUN git clone https://github.com/FFmpeg/FFmpeg.git --depth 1
WORKDIR /usr/src/FFmpeg

# Configure FFmpeg with NVIDIA + Intel (oneVPL) + VAAPI support
RUN ./configure \
    --prefix=/usr/local \
    --enable-shared \
    --enable-gpl \
    --enable-libx264 \
    --enable-libx265 \
    --enable-libvpx \
    --enable-nonfree \
    --enable-libvpl \
    --enable-vaapi \
    --extra-cflags='-I/usr/local/cuda/include' \
    --extra-ldflags='-L/usr/local/cuda/lib64' \
    --enable-cuda \
    --enable-nvenc \
    --enable-nvdec \
    --extra-libs='-lpthread -lm' && \
    make -j"$(nproc)" && make install && ldconfig

# Python runtime and web server
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN pip3 install flask gunicorn

WORKDIR /app
COPY . /app

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
