# Debian bookworm (OpenSSL 3.0), pinned rather than python:3.11-slim, which now tracks trixie
# (OpenSSL 3.5). Through the page proxy, Dotdash Meredith's Cloudflare challenged 4 of 5 sites for
# trixie's TLS handshake and served all of them for bookworm's (2026-11-15; CHANGELOG 9.35.0).
FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "recipeparser.adapters.api:app", "--host", "0.0.0.0", "--port", "8000"]
