FROM python:3.13.15-alpine3.24 AS builder
WORKDIR /work
RUN apk add --no-cache build-base libffi-dev openssl-dev
COPY pyproject.toml ./pyproject.toml
COPY src/ ./src/
# /work/deps: everything, for the control pod. /work/deps-execution: the base set only (no
# `control` extra), for the execution unit that every sandbox starts.
RUN python -m pip install --no-cache-dir --target=/work/deps ".[control]" \
    && python -m pip install --no-cache-dir --target=/work/deps-execution . \
    && for d in /work/deps /work/deps-execution; do \
         find "$d" -type d -name __pycache__ -prune -exec rm -rf {} + \
         && find "$d" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
         && find "$d" -name '*.pyc' -delete \
         && find "$d" -name '*.pyi' -delete; \
       done

FROM node:22.23.3-alpine3.24 AS opencode-builder
WORKDIR /opt/opencode
RUN npm install --no-save @opencode/cli@2.2.5 \
    && ./node_modules/.bin/opencode2 --version

FROM node:22.23.3-alpine3.24 AS pig-builder
WORKDIR /opt/pig
# npm installs a node launcher plus the native binary for this platform. Only the binary is copied,
# under the name pig: PiG refuses to run as pi.
RUN npm install --no-save @pi-in-go/pig@0.7.0 \
    && mkdir -p /opt/pi \
    && cp "$(find node_modules/@pi-in-go -type f -name pig -perm -u+x | head -1)" /opt/pi/pig \
    && /opt/pi/pig --version

FROM python:3.13.15-alpine3.24 AS runtime
WORKDIR /work
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/work/src:/work/deps
# apk upgrade picks up Alpine fixes published after the pinned base image was built.
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates git openssh-client libstdc++ libgcc tzdata \
    && rm -rf /root/.cache /tmp/* /var/cache/apk/*
COPY src/ /work/src/
COPY plugins/ /work/plugins/
# Bytecode compiled once at build (here and for each stage's /work/deps): PYTHONDONTWRITEBYTECODE
# means a sandbox would otherwise compile every imported module on each start.
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /work/src
RUN rm -rf /usr/local/lib/python3.13/ensurepip \
    /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-* \
    /usr/local/bin/pip /usr/local/bin/pip3 /root/.cache /tmp/* \
    && rm -rf /var/cache/apk/*

FROM runtime AS control
COPY --from=builder /work/deps/ /work/deps/
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /work/deps \
    && addgroup -S -g 10001 ach \
    && adduser -S -D -u 10001 -G ach -h /home/ach -s /sbin/nologin ach
ENV HOME=/home/ach
USER 10001:10001
EXPOSE 8080 8081
ENTRYPOINT ["python", "-m", "ach_runtime"]
CMD ["control", "--config", "/etc/ach-runtime/config.json"]

FROM runtime AS execution-base
COPY --from=builder /work/deps-execution/ /work/deps/
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /work/deps \
    && addgroup -S -g 10001 ach \
    && adduser -S -D -u 10001 -G ach -h /home/ach -s /sbin/nologin ach \
    && mkdir -p /sandbox/ach/workspace /sandbox/ach/sessions \
    && chown -R 10001:10001 /sandbox/ach \
    && printf '#!/bin/sh\nexec python -m ach_runtime.mcpcall "$@"\n' > /usr/local/bin/ach-mcp \
    && chmod 0755 /usr/local/bin/ach-mcp
ENV HOME=/home/ach
USER 10001:10001
EXPOSE 8080
# No VOLUME for /sandbox/ach: containerd mounts an anonymous volume over each declared path, on
# top of the Workspace PVC (persistence type volume), and the data goes with the container.
ENTRYPOINT ["python", "-m", "ach_runtime"]
CMD ["execution"]

FROM execution-base AS execution
# opencode is one self-contained native binary. npm also installs its other platform variants
# (~585 MB), so only the one the postinstall linked to cli/bin is copied; nothing needs node.
ENV OPENCODE_BIN=/opt/opencode/node_modules/@opencode/cli/bin/opencode.exe
COPY --from=opencode-builder /opt/opencode/node_modules/@opencode/cli/bin/opencode.exe /opt/opencode/node_modules/@opencode/cli/bin/opencode.exe
RUN "$OPENCODE_BIN" --version | grep -Fx "opencode v2.2.5"

FROM execution-base AS execution-pi
ENV PI_BIN=/opt/pi/pig PI_PACKAGE=@pi-in-go/pig PI_VERSION=0.7.0
COPY --from=pig-builder /opt/pi/pig /opt/pi/pig
RUN "$PI_BIN" --version | grep -Fx "0.7.0+1.5.0"

FROM execution AS agent
USER root
ENV PI_BIN=/opt/pi/pig PI_PACKAGE=@pi-in-go/pig PI_VERSION=0.7.0
COPY --from=pig-builder /opt/pi/pig /opt/pi/pig
# The pi TUI downloads these at startup when they are missing.
RUN apk add --no-cache ripgrep fd && rm -rf /var/cache/apk/*
# The whole agent in one process needs the control pod's dependencies too.
COPY --from=builder /work/deps/ /work/deps/
RUN python -m compileall -q -j0 --invalidation-mode unchecked-hash /work/deps
RUN mkdir -p /var/lib/ach-runtime/workspaces && chown 10001:10001 /var/lib/ach-runtime/workspaces
USER 10001:10001
CMD ["agent", "--config", "/etc/ach-runtime/config.json"]
