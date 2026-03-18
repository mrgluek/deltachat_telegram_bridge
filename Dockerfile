FROM python:3.11-slim

WORKDIR /app

# Install dependencies required by python-telegram-bot and deltachat
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# User and Group ID for matching host permissions
ARG UID=1000
ARG GID=1000

# Create non-root user matching the host user's UID/GID
RUN groupadd -g $GID bot || groupadd bot && \
    useradd -u $UID -g $GID -m -s /bin/bash bot || useradd -m -s /bin/bash bot

# Copy bot files
COPY . .
RUN chown -R bot:bot /app

USER bot

# Run the unbuffered Python process
CMD ["python", "-u", "bot.py", "serve"]
