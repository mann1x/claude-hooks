FROM node:22-slim

# Install episodic-memory
RUN npm install -g episodic-memory

# Copy the HTTP server
COPY server.py /app/server.py

# Python is included in node:22-slim via system
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*

EXPOSE 11435

CMD ["python3", "/app/server.py", "--port", "11435"]
