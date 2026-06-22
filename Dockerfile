FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y sqlite3 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "from pathlib import Path; import sys, time; path = Path('/tmp/jr-foxy-healthy'); sys.exit(0 if path.exists() and time.time() - path.stat().st_mtime < 90 else 1)"

CMD ["python", "main.py"]
