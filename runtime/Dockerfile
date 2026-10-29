FROM python:3.13.15-alpine3.24 AS builder
WORKDIR /work
RUN apk add --no-cache build-base libffi-dev openssl-dev
COPY pyproject.toml ./pyproject.toml
COPY src/ ./src/
RUN python -m pip install --no-cache-dir --target=/work/deps . \
    && find /work/deps -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /work/deps -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
    && find /work/deps -name '*.pyc' -delete \
    && find /work/deps -name '*.pyi' -delete

FROM node:22.23.3-alpine3.24 AS opencode-builder
WORKDIR /opt/opencode
RUN npm install --no-save @opencode/cli@2.0.20 \
    && ./node_modules/.bin/opencode2 --version

FROM python:3.13.15-alpine3.24 AS runtime
WORKDIR /work
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/work/src:/work/deps
RUN apk add --no-cache ca-certificates git openssh-client libstdc++ libgcc tzdata \
    && rm -rf /root/.cache /tmp/* /var/cache/apk/*
COPY --from=builder /work/deps/ /work/deps/
COPY src/ /work/src/
COPY plugins/ /work/plugins/
RUN rm -rf /usr/local/lib/python3.13/ensurepip \
    /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-* \
    /usr/local/bin/pip /usr/local/bin/pip3 /root/.cache /tmp/* \
    && rm -rf /var/cache/apk/*

FROM runtime AS control
RUN addgroup -S -g 10001 ach \
    && adduser -S -D -u 10001 -G ach -h /home/ach -s /sbin/nologin ach
ENV HOME=/home/ach
USER 10001:10001
EXPOSE 8080 8081
ENTRYPOINT ["python", "-m", "ach_runtime"]
CMD ["control", "--config", "/etc/ach-runtime/config.json"]

FROM runtime AS execution
ENV OPENCODE_BIN=/opt/opencode/node_modules/.bin/opencode2
COPY --from=opencode-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode-builder /opt/opencode /opt/opencode
RUN test -x /usr/local/bin/node \
    && test -x /opt/opencode/node_modules/.bin/opencode2 \
    && addgroup -S -g 10001 ach \
    && adduser -S -D -u 10001 -G ach -h /home/ach -s /sbin/nologin ach \
    && mkdir -p /workspace /var/lib/ach-runtime/sessions \
    && chown 10001:10001 /workspace /var/lib/ach-runtime/sessions \
    && printf '#!/bin/sh\nexec python -m ach_runtime.mcpcall "$@"\n' > /usr/local/bin/ach-mcp \
    && chmod 0755 /usr/local/bin/ach-mcp
ENV HOME=/home/ach
USER 10001:10001
EXPOSE 8080
VOLUME ["/workspace", "/var/lib/ach-runtime/sessions"]
ENTRYPOINT ["python", "-m", "ach_runtime"]
CMD ["execution"]

FROM execution AS agent
USER root
RUN mkdir -p /var/lib/ach-runtime/workspaces && chown 10001:10001 /var/lib/ach-runtime/workspaces
USER 10001:10001
CMD ["agent", "--config", "/etc/ach-runtime/config.json"]
