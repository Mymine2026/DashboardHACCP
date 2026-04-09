FROM python:3.11-slim

WORKDIR /app

COPY trackpac_server.py .
COPY sensori.txt .
COPY onboarding.html .
COPY azione.html .

RUN pip install --no-cache-dir psycopg2-binary && \
    mkdir -p /app/data && chmod 777 /app/data

ENV PORT=3000
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data

EXPOSE 3000

CMD ["python3", "-u", "trackpac_server.py"]
