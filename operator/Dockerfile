# syntax=docker/dockerfile:1
FROM golang:1.26-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY api/ api/
COPY cmd/ cmd/
COPY internal/ internal/
COPY third_party/ third_party/
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/operator ./cmd/operator

FROM gcr.io/distroless/static:nonroot
COPY --from=builder /out/operator /operator
USER 65532:65532
ENTRYPOINT ["/operator"]
