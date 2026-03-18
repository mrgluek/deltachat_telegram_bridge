FROM python:3.11-slim

WORKDIR /app

# Install dependencies required by python-telegram-bot and deltachat
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for running the bot
RUN useradd -m -s /bin/bash bot

# Copy bot files
COPY . .
RUN chown -R bot:bot /app

USER bot

# Run the unbuffered Python process
CMD ["python", "-u", "bot.py", "serve"]
