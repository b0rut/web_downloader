# Use official Python slim image
FROM python:3.11-slim

# Install system dependencies: ffmpeg and other useful tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY index.html .

# Create a non-root user to run the app (security)
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose the port Flask runs on
EXPOSE 5000

# Run the app with gunicorn (production-ready)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]