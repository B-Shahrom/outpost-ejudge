# One image for the web server and the judge. Compilers match electicode's
# languages: GNU C++ (g++), Java 21, Python 3.12.
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ openjdk-21-jdk-headless iproute2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY outpost ./outpost
RUN pip install --no-cache-dir .

ENV OUTPOST_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
CMD ["outpost", "serve"]
