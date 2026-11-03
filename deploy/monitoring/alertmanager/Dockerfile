# Config baked into the pinned image (see prometheus/Dockerfile for why).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY entrypoint.sh telegram.tmpl /etc/alertmanager/proxyhub/
ENTRYPOINT ["/bin/sh", "/etc/alertmanager/proxyhub/entrypoint.sh"]
CMD []
