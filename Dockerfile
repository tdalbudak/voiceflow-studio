FROM python:3.11-slim

# FFmpeg + sistem bağımlılıkları
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bağımlılıkları önce kopyala (cache için)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama dosyaları
COPY main.py .
COPY index.html .

# Çalışma klasörleri
RUN mkdir -p ciktilar gecici

# Port
EXPOSE 8000

# Başlat
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
