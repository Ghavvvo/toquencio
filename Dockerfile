# Lightweight image for the Telegram bot
FROM cgr.dev/chainguard/python:3.12-dev

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
