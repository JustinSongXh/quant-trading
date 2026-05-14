FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt fastapi uvicorn

COPY . .

EXPOSE 80

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "80"]
