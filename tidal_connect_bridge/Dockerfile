# Stage 1: the community Tidal Connect image (32-bit armhf, Raspbian stretch).
# Only used as a file source; nothing from it runs during the build.
ARG TIDAL_IMAGE=edgecrush3r/tidal-connect:latest
FROM --platform=linux/arm/v7 ${TIDAL_IMAGE} AS tidal

# Stage 2: native arm64 environment (dbus, avahi, the Python bridge).
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      avahi-daemon avahi-utils dbus ca-certificates python3 procps \
 && rm -rf /var/lib/apt/lists/*

# The armhf root filesystem lives under /armhf; the Tidal program is started
# through the armhf loader with an explicit library path (no chroot needed).
COPY --from=tidal / /armhf/
RUN [ -e /usr/share/alsa ] || ln -s /armhf/usr/share/alsa /usr/share/alsa

COPY run.sh manager.py page.html /
RUN chmod +x /run.sh

CMD ["/run.sh"]
