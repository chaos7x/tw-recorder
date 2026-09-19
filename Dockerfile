# ==========================================
# STUFE 0: Statische Binaries bereitstellen
# ==========================================
FROM mwader/static-ffmpeg:latest AS ffmpeg-binaries

# ==========================================
# STUFE 1: Builder - installiert tw_recorder + streamlink isoliert per pip
# ==========================================
# Eigene Stage, damit pip/setuptools NICHT im finalen Laufzeit-Image landen -
# nur die fertig installierten Pakete werden per COPY --from übernommen.
FROM debian:trixie-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-setuptools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Getrennte --target-Ordner je Package (nicht gemeinsam mit tw_recorder!) -
# zwei separate `pip install --target=X` Aufrufe in denselben, bereits
# befüllten Zielordner haben dazu geführt, dass beim zweiten Aufruf kein
# Entry-Point-Skript mehr erzeugt wurde (bin/tw-recorder fehlte, obwohl das
# Package selbst + dist-info korrekt installiert waren). Getrennte Ordner
# umgehen das und halten streamlinks Neben-Scripts (idna, normalizer,
# wsdump, ...) außerdem sauber von tw-recorder getrennt.
RUN pip install --break-system-packages --no-cache-dir --target=/install/streamlink streamlink

COPY pyproject.toml /build/pyproject.toml
COPY src/ /build/src/
RUN pip install --break-system-packages --no-deps --no-cache-dir --target=/install/tw-recorder /build

# ==========================================
# STUFE 2: FINAL STAGE (schlankes Laufzeit-Image, kein pip/setuptools)
# ==========================================
FROM debian:trixie-slim

# ------------------------------------------
# LAYER 1: Binaries kopieren
# ------------------------------------------
COPY --from=ffmpeg-binaries /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-binaries /ffprobe /usr/local/bin/ffprobe

# ------------------------------------------
# LAYER 2: System-Pakete
# ------------------------------------------
# apt-get upgrade: das Base-Image selbst (Pakete wie gzip/perl-base/libssl3/
# libsqlite3/libpcre2, die nicht über unsere eigenen apt-get-install-Zeilen
# kommen) hinkt Debians eigenen Security-Patches oft ein paar Tage hinterher,
# bis die Docker-Official-Images-Pipeline es neu baut - ein simples `docker
# pull` holt dann weiterhin die alte, unpatchte Version. apt-get upgrade
# zieht stattdessen bei jedem Build die aktuell in Debians eigenen Repos
# verfügbaren Paketversionen, unabhängig vom Alter des Base-Images selbst.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    python3 \
    libcom-err2 \
    mc \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------
# LAYER 2b: Dedizierter Non-Root-User
# ------------------------------------------
# Gehärtetes Image: laeuft standardmaessig nicht als root, auch wenn beim
# Deploy kein `user:`/`-u` gesetzt wird. UID/GID bewusst NICHT fest kodiert -
# adduser --system waehlt automatisch eine freie System-UID <1000, genau wie
# beim .deb-Postinst. Wer die UID an sein eigenes Setup anpassen will (z.B.
# fuer Bind-Mount-Rechte), ueberschreibt sie ganz normal per `docker run -u`/
# Compose `user:` - die 1777-Verzeichnisse unten bleiben davon unabhaengig
# fuer jede UID beschreibbar.
RUN adduser --system --group --no-create-home --home /nonexistent \
    --shell /usr/sbin/nologin tw-recorder

WORKDIR /app
ENV HOME=/app
ENV PYTHONPATH=/usr/local/lib/tw-recorder:/usr/local/lib/streamlink

# ------------------------------------------
# LAYER 3: Verzeichnisse anlegen & vorbereiten
# ------------------------------------------
# 1777 statt 777: die Laufzeit-UID ist unbekannt (frei wählbar via `docker run -u`,
# um Berechtigungskonflikte mit host-gemounteten Verzeichnissen zu vermeiden),
# daher müssen beide Verzeichnisse für jede UID beschreibbar bleiben. Das
# Sticky-Bit (wie bei /tmp) verhindert aber, dass ein Prozess/Nutzer Dateien
# löschen oder umbenennen kann, die ein anderer angelegt hat.
RUN mkdir -p /storage /log /etc/tw-recorder/conf.d /srv/media-pipeline/recordings \
    && chmod 1777 /storage /log /srv/media-pipeline/recordings

# ------------------------------------------
# LAYER 4: tw_recorder + streamlink aus dem Builder übernehmen
# ------------------------------------------
COPY --from=builder /install/tw-recorder /usr/local/lib/tw-recorder
COPY --from=builder /install/streamlink /usr/local/lib/streamlink
COPY --from=builder /install/tw-recorder/bin/tw-recorder /usr/local/bin/tw-recorder
COPY --from=builder /install/streamlink/bin/streamlink /usr/local/bin/streamlink

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

# /app gehoert dem dedizierten User statt root, damit HOME=/app (siehe oben)
# fuer ihn tatsaechlich beschreibbar ist (z.B. fuer streamlink-Caches).
RUN chown tw-recorder:tw-recorder /app
USER tw-recorder

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["tw-recorder", "--healthcheck"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

ARG VERSION
ARG BUILD_DATE

LABEL version="${VERSION}"
LABEL build_date="${BUILD_DATE}"
LABEL maintainer="Chaos7x"
LABEL purpose="Twitch stream recording automation with Streamlink and static FFmpeg"
