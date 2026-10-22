# Pat (Winlink client, MIT; github.com/la5nta/pat) for ham-modem's ARDOP lab tests.
FROM debian:bookworm-slim
ARG PAT_VERSION=1.0.0
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl \
 && curl -fsSL -o /tmp/pat.deb https://github.com/la5nta/pat/releases/download/v${PAT_VERSION}/pat_${PAT_VERSION}_linux_amd64.deb \
 && apt-get install -y -qq /tmp/pat.deb && rm -f /tmp/pat.deb && rm -rf /var/lib/apt/lists/*
RUN useradd -m pat
USER pat
WORKDIR /home/pat
