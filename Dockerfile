FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY . .

EXPOSE 8000

# Serve with gunicorn + uvicorn workers. The app creates any missing tables on
# startup (init_db); run the provider index/field migrations once separately
# (see DEPLOY_VULTR.md). $PORT defaults to 8000.
CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8000} -w ${WEB_CONCURRENCY:-3} --timeout 120"]
