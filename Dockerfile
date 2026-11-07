FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ADP_CONFIG_FILE=/app/config.toml
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 adp \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin adp

COPY src/ /app/src/
COPY main.py /app/main.py
COPY pyproject.toml /app/pyproject.toml
RUN python -m pip install --no-cache-dir --no-deps . \
    && mkdir -p /app/runtime /app/logs && chown -R adp:adp /app/runtime /app/logs
USER adp

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
