FROM python:3.11-slim

WORKDIR /app

COPY trackpac_server.py .
COPY sensori.txt .

# Set these in Dokploy > Environment Variables:
# TRACKPAC_API_KEY, SMTP_USER, SMTP_PASS
# TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
# ADMIN_USER, ADMIN_PASS, DATA_DIR=/app, PORT=3000

ENV PORT=3000
ENV PYTHONUNBUFFERED=1

EXPOSE 3000

CMD ["python3", "-u", "trackpac_server.py"]
