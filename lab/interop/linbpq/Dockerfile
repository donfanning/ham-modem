FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS build

ARG LINBPQ_COMMIT=228047d76f410e13a564fb00787516f69b96ff44

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      build-essential ca-certificates git sudo libcap2-bin libconfig-dev libminiupnpc-dev \
      libpcap-dev zlib1g-dev libjansson-dev libpaho-mqtt-dev libbacktrace-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/linbpq
RUN git init . \
 && git remote add origin https://github.com/g8bpq/linbpq.git \
 && git fetch --depth=1 origin "$LINBPQ_COMMIT" \
 && git checkout --detach FETCH_HEAD \
 && make \
 && setcap cap_net_raw,cap_net_admin,cap_net_bind_service=ep linbpq \
 && git rev-parse HEAD > SOURCE_REVISION

FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      libpaho-mqtt1.3 libjansson4 libminiupnpc18 libconfig11 libpcap0.8t64 \
      zlib1g libbacktrace0 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/linbpq
COPY --from=build /opt/linbpq/linbpq /opt/linbpq/linbpq
COPY --from=build /opt/linbpq/SOURCE_REVISION /opt/linbpq/SOURCE_REVISION
COPY bpq32.cfg /opt/linbpq/bpq32.cfg
COPY linmail.cfg /opt/linbpq/linmail.cfg
EXPOSE 8010/tcp
CMD ["./linbpq"]
