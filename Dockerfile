# ==========================================
# STUFE 0: Statische Binaries bereitstellen
# ==========================================
FROM mwader/static-ffmpeg:latest AS ffmpeg-binaries

# ==========================================
# STUFE 1: Schlankes Laufzeit-Image
# ==========================================
FROM debian:trixie-slim

# ------------------------------------------
# LAYER 1: Binaries kopieren
# ------------------------------------------
COPY --from=ffmpeg-binaries /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-binaries /ffprobe /usr/local/bin/ffprobe

# ------------------------------------------
# LAYER 2: System-Pakete + Pip-Installation
# ------------------------------------------
# Kein --target nötig: Debians python3-pip ist patched und installiert bei
# einem normalen `pip install` bereits automatisch nach /usr/local/bin bzw.
# /usr/local/lib/python3.XX/dist-packages - genau dort, wo der System-Python
# auch sucht. Das --target/PYTHONPATH/Symlink-Muster war nötig, um einen
# echten Bug auf Alpine zu umgehen (vanilla pip dort installiert relativ zu
# /usr statt /usr/local) - auf Debian besteht dieses Problem nicht.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-setuptools \
    libcom-err2 \
    mc \
    ca-certificates \
    && pip install --no-cache-dir --break-system-packages streamlink \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV HOME=/app

# ------------------------------------------
# LAYER 3: Verzeichnisse anlegen & vorbereiten
# ------------------------------------------
RUN mkdir -p /storage /log /etc/tw-recorder/conf.d \
    && chmod 777 /storage /log

# ------------------------------------------
# LAYER 4: tw_recorder-Package installieren
# ------------------------------------------
COPY pyproject.toml /app/pyproject.toml
COPY src/ /app/src/
RUN pip install --no-cache-dir --break-system-packages --no-deps . \
    && rm -rf /app/pyproject.toml /app/src /app/build

# ------------------------------------------
# LAYER 5: Skripte & Configs kopieren
# ------------------------------------------
COPY --chmod=644 bashrc /etc/global.bashrc
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=644 recorder.conf.example /etc/tw-recorder/recorder.conf

# ------------------------------------------
# LAYER 6: Symlinks erstellen
# ------------------------------------------
RUN ln -s /etc/global.bashrc /tmp/.bashrc \
    && ln -s /etc/global.bashrc /app/.bashrc

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["tw-recorder", "--healthcheck"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

ARG VERSION
ARG BUILD_DATE

LABEL version="${VERSION}"
LABEL build_date="${BUILD_DATE}"
LABEL maintainer="Chaos7x"
LABEL purpose="Twitch stream recording automation with Streamlink and static FFmpeg"
