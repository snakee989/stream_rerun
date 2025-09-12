# =========================
# Builder: oneVPL + FFmpeg
# =========================
FROM ubuntu:24.04 AS builder
ARG DEBIAN_FRONTEND=noninteractive

# Enable Ubuntu components
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common ca-certificates \
  && add-apt-repository -y universe \
  && add-apt-repository -y multiverse \
  && add-apt-repository -y restricted \
  && apt-get update \
  && rm -rf /var/lib/apt/lists/*

# Install ALL dependencies first
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake libtool nasm yasm pkg-config \
    meson ninja-build python3 git \
    gcc-11 g++-11 \
    libdrm-dev \
    libsrt-openssl-dev libssl-dev \
    libx264-dev \
    libva-dev \
    libvpl-dev \
    nvidia-cuda-dev \
    nvidia-cuda-toolkit \
    libnvidia-encode-550 \
    libnvidia-decode-550 \
  && rm -rf /var/lib/apt/lists/*

# Use GCC 11 for CUDA compatibility
RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-11 100 \
 && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-11 100

# Set environment variables
ENV PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:/usr/lib/x86_64-linux-gnu/pkgconfig"
ENV PATH="/usr/bin:/usr/local/bin:${PATH}"
ENV LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/usr/local/lib"

WORKDIR /usr/src

# Build oneVPL from source (compatible with Ubuntu 24.04's libva 2.20.0)
RUN git clone https://github.com/intel/libvpl.git --branch v2.12.0 --depth 1 \
 && cmake -S libvpl -B libvpl/build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr/local \
 && cmake --build libvpl/build -j"$(nproc)" \
 && cmake --install libvpl/build \
 && ldconfig \
 && rm -rf libvpl

# NVIDIA codec headers
RUN git clone https://github.com/FFmpeg/nv-codec-headers.git --depth 1 \
 && make -C nv-codec-headers install \
 && rm -rf nv-codec-headers

# FFmpeg source
RUN git clone https://github.com/FFmpeg/FFmpeg.git --depth 1
WORKDIR /usr/src/FFmpeg

# Configure FFmpeg
RUN ./configure \
    --prefix=/usr/local \
    --enable-gpl \
    --enable-nonfree \
    --enable-openssl \
    --enable-libsrt \
    --enable-libvpl \
    --enable-vaapi \
    --enable-cuda \
    --enable-nvenc \
    --enable-nvdec \
    --enable-libx264 \
    --pkg-config-flags="--static" \
    --extra-cflags="-I/usr/include/cuda" \
    --extra-ldflags="-L/usr/lib/x86_64-linux-gnu" \
    --disable-doc \
    --disable-debug

# Build FFmpeg
RUN make -j2 \
 && make install \
 && ldconfig

# =========================
# Final: minimal runtime
# =========================
FROM ubuntu:24.04 AS runtime
ARG DEBIAN_FRONTEND=noninteractive

ENV LIBVA_DRIVER_NAME=iHD

# Enable Ubuntu components
RUN apt-get update && apt-get install -y --no-install-recommends software-properties-common ca-certificates \
 && add-apt-repository -y universe \
 && add-apt-repository -y multiverse \
 && add-apt-repository -y restricted \
 && apt-get update \
 && rm -rf /var/lib/apt/lists/*

# Runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libdrm2 \
    libsrt1.5-openssl \
    libx264-164 \
    python3-minimal \
    libnvidia-encode-550 \
    libnvidia-decode-550 \
    libcudart12 \
    libva2 libva-drm2 \
    intel-media-va-driver-non-free \
    libvpl2 libmfx1 \
    libigdgmm12 \
    python3-flask \
    python3-gunicorn \
  && rm -rf /var/lib/apt/lists/*

# Copy FFmpeg from builder
COPY --from=builder /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=builder /usr/local/lib/ /usr/local/lib/
RUN ldconfig

# App
WORKDIR /app
COPY . /app
RUN mkdir -p /app/videos

EXPOSE 5000
CMD ["python3", "-m", "gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
