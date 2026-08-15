# Optional — only needed if you want real system ffmpeg/ffprobe instead of
# relying on the imageio-ffmpeg pip fallback (see README.md's "Deploying to
# Render" section). To use this: on Render, set the service's Language to
# "Docker" instead of "Python" — Render will build and run this file
# instead of using render.yaml's buildCommand/startCommand.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Render injects $PORT at runtime; gunicorn must bind to it.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
