FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      build-essential rsync libncurses-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/jnos
RUN rsync -a rsync://www.langelaar.net/official/2.0q/ /opt/jnos/ \
 && ./configure \
 && make -j2 jnos \
 && strip ./jnos \
 && printf '%s\n' 'JNOS source: rsync://www.langelaar.net/official/2.0q/ (2.0q)' > SOURCE_REVISION

FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      ca-certificates socat libncurses6 libssl3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/jnos
COPY --from=build /opt/jnos/jnos /opt/jnos/jnos
COPY --from=build /opt/jnos/SOURCE_REVISION /opt/jnos/SOURCE_REVISION
COPY autoexec.nos /opt/jnos/autoexec.nos
COPY domain.txt /opt/jnos/domain.txt
COPY forward.bbs /opt/jnos/spool/forward.bbs
COPY rewrite /opt/jnos/spool/rewrite
COPY run-jnos /usr/local/bin/run-jnos
RUN chmod +x /usr/local/bin/run-jnos

ENTRYPOINT ["/usr/local/bin/run-jnos"]
