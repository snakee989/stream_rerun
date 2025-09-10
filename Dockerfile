# Use Alpine-based Python for small size
FROM python:3.10-alpine

# Set working directory
WORKDIR /app

# Install runtime dependencies
RUN apk add --no-cache \
    bash \
    ffmpeg \
    tini \
    libstdc++ \
    && adduser -D appuser

# Copy Python dependencies first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire app (all in same folder)
COPY . .

# Make sure templates are accessible
# All files (app.py, index.html, etc.) are in /app

# Switch to non-root user
USER appuser

# Expose Flask port
EXPOSE 8080

# Use tini as PID 1 to handle signals correctly
ENTRYPOINT ["/sbin/tini", "--"]

# Run Flask app
CMD ["python", "app.py"]
