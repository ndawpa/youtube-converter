FROM python:3.12-slim

# Install ffmpeg and Node.js (needed for yt-dlp's JS challenge solver)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Opt-in because Whisper/PyTorch substantially increase image size and memory use.
ARG INSTALL_WHISPER=false
COPY requirements-whisper.txt .
RUN if [ "$INSTALL_WHISPER" = "true" ]; then \
        pip install --no-cache-dir -r requirements-whisper.txt; \
    fi

COPY main.py .
COPY clip_engine.py .
COPY static/ static/

RUN mkdir -p downloads

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
