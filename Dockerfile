FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin adp

COPY adp_chat/ /app/adp_chat/
COPY main.py /app/main.py
RUN mkdir -p /app/runtime /app/logs && chown -R adp:adp /app/runtime /app/logs
USER adp

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "adp_chat.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
