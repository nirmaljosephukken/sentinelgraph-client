# SentinelGraph (client edition) on Cloud Run: console with sign-in + Google ADK agent; the same image runs the worker.
# Secrets (TG_SECRET, GEMINI_API_KEY) come from Secret Manager at deploy time; nothing secret is baked in.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8080
# Cloud Run sets $PORT. Streamlit needs websockets, so deploy with --session-affinity.
CMD ["sh", "deploy/entrypoint.sh"]
