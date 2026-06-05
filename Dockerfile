FROM python:3.11-slim

WORKDIR /app

# Install dependencies required by python-telegram-bot and deltachat
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bin/deltachat-rpc-server /usr/local/bin/deltachat-rpc-server
COPY . .


# Run the unbuffered Python process
CMD ["python", "-u", "bot.py", "serve"]
