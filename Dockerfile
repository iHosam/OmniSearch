FROM python:3.12-alpine

WORKDIR /app

# Create data directory for SQLite database
RUN mkdir -p /app/data

COPY server.py /app/server.py

EXPOSE 5050

ENV PORT=5050 \
    DB_PATH=/app/data/omnisearch.db \
    PYTHONUNBUFFERED=1

CMD ["python", "/app/server.py"]
