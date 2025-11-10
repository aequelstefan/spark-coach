FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY coach.py .
COPY service.py .
COPY data/ data/

# Run the service
CMD ["python", "service.py"]
