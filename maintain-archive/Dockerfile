FROM alpine:3.20

ARG TOFU_VERSION=1.9.0

RUN apk add --no-cache ca-certificates unzip wget && \
    wget -O /tmp/tofu.zip \
      "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" && \
    unzip /tmp/tofu.zip tofu -d /usr/local/bin && \
    rm /tmp/tofu.zip && \
    tofu version
