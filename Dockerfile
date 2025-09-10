# Use Python 3.10 slim image
FROM python:3.10-slim

# Install ffmpeg (CPU version) and other dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy application
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose Flask port
EXPOSE 8080

# Run the app
CMD ["python", "app.py"]
