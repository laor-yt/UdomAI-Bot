FROM python:3.11-slim

# Set environment variables for non-interactive debian installations and clean output
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore

# Install system dependencies (ffmpeg, curl, ca-certificates, nodejs) quietly without apt CLI warnings
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends curl ca-certificates gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirement files and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -U yt-dlp

# Copy source code
COPY . .

# Run the bot
CMD ["python", "main.py"]

