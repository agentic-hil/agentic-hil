# TLS-proxy bench evaluation

One Docker image that **is** the bench: a Linux machine behind a TLS-inspecting
proxy, where `agentic-hil upgrade` fails and the one-line installer gets through.

## The bench

A Linux bench sits behind a TLS-inspecting proxy. Agentic HIL 0.16.0 is
installed there through `uv`. Asking it to upgrade itself ends like this:

```
error: Failed to upgrade agentic-hil
  Caused by: Failed to fetch: `https://pypi.org/simple/agentic-hil/`
  Caused by: invalid peer certificate: UnknownIssuer
```

Nothing else on that machine has a problem. `curl` works, `apt` works, the
browser works. The asymmetry is the whole story: `uv` validates the chain
against roots compiled into its own binary, and the proxy's CA is not one of
them. The machine's own trust store *does* carry that CA, which is why every
other program on the box is happy.

The one-line installer already knows this shape. A failed install whose text
says the chain ended outside the roots it was checking against is retried once
with `UV_SYSTEM_CERTS=1`, so `uv` reads the machine's own store instead, with
verification still on (`trust_failure()` in `install.sh`, #293). `agentic-hil
upgrade` does not know it yet, which is why an operator on such a bench can
install but cannot upgrade.

## What the container does

The image builds that bench from nothing:

- `mitmdump` as an ordinary HTTP(S) proxy on `127.0.0.1:8080`;
- its CA generated at build time and installed into the system store with
  `update-ca-certificates`, so the machine trusts the proxy;
- `HTTP_PROXY` and `HTTPS_PROXY` exported for every program in the image;
- `uv`, installed before any of that, the way the bench already had it, from the
  installer version and hash `install.sh` itself pins.

Then the entrypoint runs three steps in order and stops at the first one that
fails:

1. **Seed.** `UV_SYSTEM_CERTS=1 uv tool install agentic-hil==0.16.0`. This
   provisions the bench and proves the system-store path works at all: the same
   proxy, the same index, one variable set for one command.
2. **Proof 1, the failure.** `agentic-hil upgrade --json` with that variable
   removed from the environment. The run asserts the report carries
   `upgrade_failed` and that the manager stderr it recorded contains both
   `invalid peer certificate` and `UnknownIssuer`, and prints the whole report
   and the whole error. Nothing is filtered: the certificate error is the point.
3. **Proof 2, the anchor repairs it.** The released one-line installer, fetched
   and run exactly as an operator would type it:
   `curl -LsSf https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.sh | sh`.
   The run asserts that the installer met the same certificate failure, said it
   was switching to this machine's own store, exited zero, and left the current
   release installed and answering `--version`.

Each step prints a `PASS:` or `FAIL:` line with the decisive evidence line under
it. The first failure ends the run non-zero.

## Running it

Two commands, from the repository root:

```sh
docker build --file evals/tls_proxy/container/Dockerfile --tag agentic-hil-tls-proxy-eval:local .
docker run --rm agentic-hil-tls-proxy-eval:local
```

The run needs outbound network: it talks to PyPI and to GitHub, through its own
proxy. It takes a few minutes. Nothing is written outside the container, and
`--rm` removes it either way.

Under pytest the same build and run live in
`tests/test_tls_proxy_eval.py::test_the_container_reproduces_both_halves_of_the_bench`,
which skips unless Docker is present **and** `AGENTIC_HIL_TLS_PROXY_EVAL=1` is
set. It is not part of default CI, for the same reason it is opt-in here: it
costs network and minutes. Set `AGENTIC_HIL_TLS_PROXY_EVAL_IMAGE` to build and
run under a different tag. The rest of that file reads the committed files here
and runs everywhere.

## Trust boundary

The CA is generated inside the image, is trusted by that one container, and its
key never leaves it. The base image is pinned by digest and the uv bootstrap is
pinned by version and checked against its hash before it runs, so nothing the
image installs is a moving download piped into a shell. Nothing here relaxes
certificate verification: there is no `--insecure`, no `--trusted-host`, no
`SSL_CERT_FILE`, and a test asserts there never will be. Both attempts a run
makes verify the chain; the only thing that changes between them is which store
the chain is checked against.

## What comes next

#326 teaches `agentic-hil upgrade` the same move the installer already makes:
recognise a trust failure and retry once against the machine's own store. When
it lands, this eval gains a third proof, run after proof 1 and before proof 2:
the upgrade self-heals, so the operator on a proxied bench never has to reach
for the installer at all. Until then proof 1 is expected to fail, and it failing
is the reason the file exists.
