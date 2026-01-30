FROM python:3.9-slim

WORKDIR /app

# Install system dependencies if needed (e.g. for numpy/pandas compilation, though wheels usually exist)
# RUN apt-get update && apt-get install -y ...

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .
# Copy weights from parent or assume they are copied to build context
# We will assume build context includes everything or we copy explicitly
# For now, let's assume we copy them in.

CMD ["python", "worker.py"]
