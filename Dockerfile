FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && pip install -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY bot.py engine.py grid.py ./

CMD ["python", "bot.py"]
