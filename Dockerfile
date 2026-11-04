# Gerbonara requires Python 3.12; KiCad's Debian 12 image ships Python 3.11.
# Both stages use Bookworm to keep the copied interpreter ABI compatible.
FROM python:3.12.12-slim-bookworm AS python-runtime

# Official KiCad 9 image. Build from a trusted PCBGit source revision.
FROM kicad/kicad:9.0.9

USER root
RUN apt-get update && apt-get install -y --no-install-recommends git \
    libssl3 libbz2-1.0 liblzma5 zlib1g libffi8 libsqlite3-0 libreadline8 \
    libncursesw6 libgdbm6 libexpat1 libuuid1 libgl1 libx11-6 libxrender1 libxext6 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-runtime /usr/local/ /usr/local/
RUN ldconfig && python3.12 -c 'import ssl, sqlite3, bz2, lzma, zlib'
RUN kicad-cli version
WORKDIR /opt/pcbgit-source
COPY pyproject.toml README.md ./
COPY pcbgit ./pcbgit
RUN python3.12 -m venv /opt/pcbgit && /opt/pcbgit/bin/pip install --no-cache-dir '.[geometry]' \
    && /opt/pcbgit/bin/python -c 'import OCP, gerbonara'
ENV PATH="/opt/pcbgit/bin:${PATH}" HOME=/tmp
WORKDIR /workspace
ENTRYPOINT ["pcbgit"]
