FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY sidecar ./sidecar

RUN pip install --no-cache-dir .

EXPOSE 8765

CMD ["uvicorn", "sidecar.gateway.main:app", "--host", "0.0.0.0", "--port", "8765"]
