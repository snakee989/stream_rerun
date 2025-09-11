# ---------- build stage ----------
FROM nvidia/cuda:12.2.0-devel-ubuntu22.04 AS build
ENV DEBIAN_FRONTEND=noninteractive

# Core build deps and codec dev headers (build-only)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg wget \
    build-essential pkg-config git cmake libtool automake \
    nasm yasm \
    libx264-dev libx265-dev libvpx-dev \
    libdrm-dev \
  && rm -rf /var/lib/apt/lists/*

# Intel GPU repo (jammy) for newer oneVPL/libva headers (fixes libvpl>=2.6 and QSV init)
RUN wget -qO - https://repositories.intel.com/gpu/intel-graphics.key \
    | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu jammy unified" \
      > /etc/apt/sources.list.d/intel-gpu-jammy.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      libvpl-dev \
      intel-media-va-driver-non-free \
      libva-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

# NVENC headers (required for --enable-nvenc)
RUN git clone https://github.com/FFmpeg/nv-codec-headers.git --depth 1 && \
    make -C nv-codec-headers install && rm -rf nv-codec-headers

# FFmpeg build (NVENC + NVDEC + oneVPL/QSV + VAAPI)
RUN git clone https://github.com/FFmpeg/FFmpeg.git --depth 1
WORKDIR /usr/src/FFmpeg
RUN ./configure \
    --prefix=/usr/local \
    --enable-shared \
    --enable-gpl \
    --enable-libx264 \
    --enable-libx265 \
    --enable-libvpx \
    --enable-vaapi \
    --enable-libvpl \
    --enable-nonfree \
    --enable-cuda \
    --enable-nvenc \
    --enable-nvdec \
    --extra-cflags='-I/usr/local/cuda/include' \
    --extra-ldflags='-L/usr/local/cuda/lib64' \
    --extra-libs='-lpthread -lm' && \
    make -j"$(nproc)" && make install && ldconfig

# ---------- runtime stage ----------
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive

# Add Intel GPU repo (runtime) and install VA-API media driver + minimal runtimes
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg wget \
  && rm -rf /var/lib/apt/lists/* && \
    wget -qO - https://repositories.intel.com/gpu/intel-graphics.key \
    | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu jammy unified" \
      > /etc/apt/sources.list.d/intel-gpu-jammy.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      intel-media-va-driver-non-free \
      libva2 libdrm2 \
      libx264-163 libx265-199 libvpx7 \
      python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# FFmpeg from build stage
COPY --from=build /usr/local /usr/local
RUN ldconfig && pip3 install --no-cache-dir flask gunicorn

WORKDIR /app
COPY . /app

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
