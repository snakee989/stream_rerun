# Base image
FROM ubuntu:22.04

# Install dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install Flask
RUN pip3 install flask

# Set working directory
WORKDIR /app

# Copy all project files into /app inside container
COPY . /app/

# Expose Flask port
EXPOSE 8080

# Start Flask app
CMD ["python3", "app.py"]
