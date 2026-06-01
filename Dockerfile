FROM python:3.12-slim

ARG PIP_INDEX_URL

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    libgtk-3-0 libx11-xcb1 wget tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN test -n "${PIP_INDEX_URL}" \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt \
    && playwright install chromium --with-deps

COPY backend ./backend

ENV PYTHONUNBUFFERED=1
ENV UPLOAD_DIR=/tmp/data_agent_uploads

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
