FROM ubuntu:24.04

ENV container=docker
COPY supervisor-requirements.txt /tmp/supervisor-requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates dbus python3-venv systemd util-linux \
    && useradd --system --user-group --no-create-home fertile-drill \
    && python3 -m venv /opt/fertile-control \
    && /opt/fertile-control/bin/python -m pip install --no-cache-dir -r /tmp/supervisor-requirements.txt \
    && apt-get clean
