# The e2e control image trusts the exchange stub's CA; httpx reads SSL_CERT_FILE.
ARG BASE=ach-agent-control:e2e
FROM ${BASE}
COPY ca.crt /etc/ach-e2e/ca.crt
ENV SSL_CERT_FILE=/etc/ach-e2e/ca.crt
