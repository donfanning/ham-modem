# ardopcf (MIT; github.com/pflarue/ardop) as the ARDOP reference peer for the ham-modem lab.
FROM debian:bookworm-slim
# bookworm (GCC 12): trixie's GCC 14 makes -Wint-conversion an error in the bundled lib/rawhid.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends build-essential libasound2-dev git ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*
ARG ARDOP_REF=master
RUN git clone --depth 1 --branch ${ARDOP_REF} https://github.com/pflarue/ardop.git /src/ardop \
 && make -C /src/ardop -j4 && install -m 755 /src/ardop/ardopcf /usr/local/bin/ardopcf
RUN useradd -m ardop
USER ardop
WORKDIR /home/ardop
