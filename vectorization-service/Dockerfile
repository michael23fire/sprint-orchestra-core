# Docling pulls in torch + ML models, so use the full python image (slim lacks build deps some
# transitive wheels need) and keep the layer cache friendly by installing deps before app code.
FROM python:3.11

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations

EXPOSE 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
