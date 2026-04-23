FROM python:3.12-slim

# ffmpeg for whisper audio decode
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

VOLUME ["/data"]
ENV SQLITE_PATH=/data/calls.db \
    RECORDINGS_DIR=/data/recordings \
    SESSION_FILE=/data/session.json \
    WHISPER_BACKEND=faster-whisper \
    WHISPER_MODEL=tiny

ENTRYPOINT ["python", "unifi_talk.py"]
CMD ["--loop", "300", "sync", "--transcribe", "--db", "/data/calls.db", "--recordings-dir", "/data/recordings"]
