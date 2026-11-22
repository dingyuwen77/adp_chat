FROM python:3.14-slim AS sqlite-build

# Debian slim 的系统 SQLite 版本不含 WAL-reset 修复。仅打包官方校验后的共享库。
ARG DEBIAN_MIRROR=https://mirrors.ustc.edu.cn
RUN sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 update \
    && apt-get install -y --no-install-recommends gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*
ADD https://sqlite.org/2026/sqlite-autoconf-3540100.tar.gz /tmp/sqlite.tar.gz
RUN python -c "import hashlib, pathlib; expected='b6ced585c750916b1052667239a12c25a7ff34e7f06e1e6e01ecb0427657d9c4'; assert hashlib.sha3_256(pathlib.Path('/tmp/sqlite.tar.gz').read_bytes()).hexdigest() == expected" \
    && mkdir /tmp/sqlite \
    && tar -xzf /tmp/sqlite.tar.gz -C /tmp/sqlite --strip-components=1 \
    && cd /tmp/sqlite \
    && ./configure --disable-static --disable-readline \
    && make -j2 libsqlite3.so \
    && install -D -m 0755 libsqlite3.so /opt/sqlite/libsqlite3.so.0

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ADP_CONFIG_FILE=/app/config.toml
WORKDIR /app

COPY --from=sqlite-build /opt/sqlite/libsqlite3.so.0 /usr/local/lib/
RUN ldconfig \
    && python -c "import sqlite3; print('SQLite', sqlite3.sqlite_version); assert sqlite3.sqlite_version_info >= (3, 51, 3)"

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 adp \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin adp

COPY src/ /app/src/
COPY main.py /app/main.py
COPY pyproject.toml /app/pyproject.toml
RUN python -m pip install --no-cache-dir --no-deps . \
    && mkdir -p /app/runtime /app/logs && chown -R adp:adp /app/runtime /app/logs
USER adp

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
