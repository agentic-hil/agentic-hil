# One container that is the bench: a Linux machine behind a TLS-inspecting proxy.
#
# Two facts make that bench, and this file installs both. The machine's own
# trust store knows the proxy's CA, so curl and apt keep working. uv validates
# against roots compiled into its own binary, which have never heard of that CA,
# so uv is the one program on the box that cannot reach the index.
#
# Pinned by digest, not by tag: a tag is a moving target, and an eval that
# silently changes its base image is an eval whose results cannot be compared
# across runs. Dependabot's docker ecosystem keeps the digest fresh.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

# mitmproxy is the proxy. curl is how the released one-line installer is fetched
# and is also half the evidence: curl reads this machine's own store, so the
# interception is invisible to it while uv trips over it. python3 reads the
# upgrade report and waits for the proxy's listening socket.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl mitmproxy python3 \
    && rm -rf /var/lib/apt/lists/*

ENV MITMPROXY_CONFDIR=/opt/tls-proxy/mitmproxy \
    TLS_PROXY_HOST=127.0.0.1 \
    TLS_PROXY_PORT=8080

# The proxy's CA, minted here and installed where this machine looks for roots.
# Generated through the same code path mitmdump runs on its first start, so the
# proxy that comes up at run time adopts this CA instead of minting a second one
# nobody installed. The private key never leaves the image and the image is a
# throwaway: this CA is trusted by exactly one container, for exactly one run.
#
# update-ca-certificates only reads *.crt out of /usr/local/share/ca-certificates,
# so the PEM is copied under that name rather than linked under its own.
RUN install -d "$MITMPROXY_CONFDIR" \
    && python3 -c "from mitmproxy.certs import CertStore; CertStore.from_store('$MITMPROXY_CONFDIR', 'mitmproxy', 2048)" \
    && cp "$MITMPROXY_CONFDIR/mitmproxy-ca-cert.pem" /usr/local/share/ca-certificates/mitmproxy-bench-ca.crt \
    && update-ca-certificates

RUN useradd --create-home --user-group --shell /bin/bash bench \
    && chown -R bench:bench /opt/tls-proxy

COPY evals/tls_proxy/container/entrypoint.sh /opt/tls-proxy/entrypoint.sh
RUN chmod 0755 /opt/tls-proxy/entrypoint.sh

# The package under review. Proof 2 installs a released agentic-hil by name --
# so `uv tool upgrade` fetches the index and meets the proxy the way proof 1
# does -- and then overlays this source over it, so the upgrade it runs is the
# code in this checkout rather than any release. Only the package is copied, not
# the whole tree: the entrypoint places it into the tool environment it builds.
COPY src/agentic_hil /opt/tls-proxy/candidate/agentic_hil

USER bench
WORKDIR /home/bench

# uv, fetched the way the bench got it: user-local, before any of this run's
# proxying is in the environment. Astral's own installer goes through curl, so
# it would survive the proxy too; it runs here so that the run itself measures
# agentic-hil rather than uv's bootstrap.
#
# Pinned to the version `install.sh` pins and checked against the hash
# `install.sh` carries, for the two reasons that file gives: the moving
# https://astral.sh/uv/install.sh serves whatever is current, and piping a
# download nobody checked into a shell is the one thing this project's
# installers refuse to do. It is also what makes two runs of this eval
# comparable, the same argument as the digest above. Bump both together.
ARG UV_INSTALLER_VERSION=0.12.5
ARG UV_INSTALLER_SHA256=504511fbbbd811aeaba6738abc79408956b6c7da0ca35437b3dcc24a41efc111
RUN curl -LsSf "https://astral.sh/uv/${UV_INSTALLER_VERSION}/install.sh" -o /tmp/uv-installer.sh \
    && echo "${UV_INSTALLER_SHA256}  /tmp/uv-installer.sh" | sha256sum -c - \
    && sh /tmp/uv-installer.sh \
    && rm /tmp/uv-installer.sh

# From this line on the environment is the bench: every outbound request from
# any program in this image goes through mitmproxy on loopback, and nothing is
# exempted. There is no variable here that turns certificate verification off,
# and none that hands uv the system store either; the run has to earn that.
ENV HOME=/home/bench \
    PATH=/home/bench/.local/bin:/usr/local/bin:/usr/bin:/bin \
    HTTP_PROXY=http://127.0.0.1:8080 \
    HTTPS_PROXY=http://127.0.0.1:8080 \
    http_proxy=http://127.0.0.1:8080 \
    https_proxy=http://127.0.0.1:8080

ENTRYPOINT ["/opt/tls-proxy/entrypoint.sh"]
