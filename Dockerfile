FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1

WORKDIR /app

# 의존성을 먼저 넣어야 소스만 바뀔 때 레이어 캐시가 산다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 인증키와 데이터는 이미지에 굽지 않고 실행 시 마운트한다.
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
