FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG BUILD_ID=1
COPY . .

# Fail the build early if the app can't even be imported.
RUN python -c "import app; print('app loaded ok')"

# Persistent data directory (mounted by Contextone)
RUN mkdir -p /app/data

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
