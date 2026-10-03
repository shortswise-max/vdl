# Python 3.10 ka base image
FROM python:3.10-slim

# System tools and dos2unix install karna
RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    git \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sab kuch copy karo
COPY . .

# CRUCIAL FIX: start.sh ke line endings fix karna aur permission dena
RUN dos2unix start.sh
RUN chmod +x start.sh

CMD ["./start.sh"]
