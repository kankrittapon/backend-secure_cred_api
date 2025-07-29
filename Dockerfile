# Base image
FROM python:3.11-slim

# Set working dir
WORKDIR /app

# Copy code
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port and run
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
