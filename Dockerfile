# --- build the dashboard ---
FROM node:22-alpine AS web
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- api ---
FROM python:3.12-slim
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 WARDEN_DB=/tmp/warden.db
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /app/backend/static ./static
EXPOSE 8000
CMD ["sh", "-c", "uvicorn warden.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
