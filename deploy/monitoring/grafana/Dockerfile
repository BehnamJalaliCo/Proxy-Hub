# Provisioning (datasources, dashboards) baked into the pinned image (see prometheus/Dockerfile).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /etc/grafana/dashboards/
