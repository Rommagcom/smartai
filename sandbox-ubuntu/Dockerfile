FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server \
        sudo \
        ca-certificates \
        curl \
        iproute2 \
        procps \
        nano \
        vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
