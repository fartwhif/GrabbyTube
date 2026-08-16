FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wireguard \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/output /etc/wireguard

# Copy WireGuard config template
COPY wireguard/wg0.conf.template /etc/wireguard/wg0.conf.template

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8101

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py"]
