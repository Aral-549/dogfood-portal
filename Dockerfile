FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY fixtures.json .
RUN useradd --system --uid 10001 dogfood && mkdir /data && chown dogfood /data
USER dogfood
ENV PYTHONPATH=/app/src DOGFOOD_DATA=/data DOGFOOD_FIXTURES=/app/fixtures.json
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "dogfood.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
