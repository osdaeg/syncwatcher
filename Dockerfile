FROM python:3.12-slim

LABEL maintainer="SyncWatcher"
LABEL description="Script post-sincronización de Syncthing"

RUN pip install requests --no-cache-dir

WORKDIR /app

COPY syncwatcher.py .

RUN mkdir -p /var/log/syncwatcher

CMD ["python3", "-u", "syncwatcher.py"]
