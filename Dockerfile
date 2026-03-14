FROM python:3.11-slim

WORKDIR /app

# Copy application
COPY trackpac_server.py .

# clients.json and alerts.json will be created at runtime in /app
# Mount a volume on /app to persist data across restarts

EXPOSE 8765

CMD ["python3", "-u", "trackpac_server.py"]
