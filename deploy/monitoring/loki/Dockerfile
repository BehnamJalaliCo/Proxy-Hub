# Config baked into the pinned image (see prometheus/Dockerfile for why).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY loki.yml /etc/loki/proxyhub.yml
CMD ["-config.file=/etc/loki/proxyhub.yml"]
