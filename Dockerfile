# Debian bullseye (glibc 2.31) on purpose: glibc 2.34+ breaks thread creation under the
# old seccomp profile of the Docker 20.10.3 shipped with DSM 6.
FROM python:3.11-slim-bullseye

LABEL org.opencontainers.image.title="SynoPixtri" \n      org.opencontainers.image.description="Automatic photo and video sorter for Synology NAS" \n      org.opencontainers.image.source="https://github.com/galaticlag/synopixtri" \n      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Bullseye is end of life: its packages now live on archive.debian.org.
RUN printf 'deb http://archive.debian.org/debian bullseye main\ndeb http://archive.debian.org/debian-security bullseye-security main\n' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV SYNOPIXTRI_PHOTOS_ROOT=/photos \
    SYNOPIXTRI_DATA_DIR=/data \
    SYNOPIXTRI_PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/photos", "/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/openapi.json', timeout=4).status == 200 else 1)"

CMD ["synopixtri"]
