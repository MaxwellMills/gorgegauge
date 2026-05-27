# Use official Playwright Python image with browsers preinstalled
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ ./src/

# Default command: run the scraper
CMD ["python", "-m", "src.scraper"]

