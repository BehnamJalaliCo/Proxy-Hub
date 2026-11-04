# Config baked into the pinned image, so a config change in git changes the image and the deployer
# (compose build + up -d) recreates the container. A bind mount would keep running the old config.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY prometheus.yml /etc/prometheus/prometheus.yml
COPY rules/ /etc/prometheus/rules/
