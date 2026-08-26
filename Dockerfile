FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create data directory for SQLite and vault ZIPs
RUN mkdir -p data/vaults

# Render injects PORT; expose it
EXPOSE ${PORT:-8443}

CMD ["python", "main.py"]
