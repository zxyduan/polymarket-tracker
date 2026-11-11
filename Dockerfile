FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
COPY framework ./framework
COPY features ./features
COPY config ./config

CMD ["python", "-m", "src.main", "--config", "config/config.yaml"]
