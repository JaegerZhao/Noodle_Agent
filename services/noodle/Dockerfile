FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NOODLE_DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY .env.example ./

EXPOSE 8000

CMD ["noodle", "next-api", "--data-dir", "/app/data", "--host", "0.0.0.0", "--port", "8000"]
