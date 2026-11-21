FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DB_PATH=/data/errata.db \
    PORT=8000
EXPOSE 8000
VOLUME ["/data"]

CMD ["python", "app.py"]
