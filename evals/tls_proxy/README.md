# TLS-proxy bench evaluation

One Docker image that **is** the bench: a Linux machine behind a TLS-inspecting
proxy, where the released 0.16.0 `agentic-hil upgrade` fails, the upgrade in this
checkout retries against the machine's own store and gets through, and the
one-line installer does the same for an operator still on the older release.

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
upgrade` now makes the same move (#326): a manager that fails with that
signature is run a second time against this machine's own store, verification
still on. The seed this container installs is a *released* version from before
that fix, so on the bench built here the upgrade still fails at proof 1, the
behaviour an operator on 0.16.0 still meets, and the reason the eval seeds it.

## What the container does

The image builds that bench from nothing:

- `mitmdump` as an ordinary HTTP(S) proxy on `127.0.0.1:8080`;
- its CA generated at build time and installed into the system store with
  `update-ca-certificates`, so the machine trusts the proxy;
- `HTTP_PROXY` and `HTTPS_PROXY` exported for every program in the image;
- `uv`, installed before any of that, the way the bench already had it, from the
  installer version and hash `install.sh` itself pins.

Then the entrypoint runs four steps in order and stops at the first one that
fails:

1. **Seed.** `UV_SYSTEM_CERTS=1 uv tool install agentic-hil==0.16.0`. This
   provisions the bench and proves the system-store path works at all: the same
   proxy, the same index, one variable set for one command.
2. **Proof 1, the failure.** `agentic-hil upgrade --json` with that variable
   removed from the environment. The run asserts the report carries
   `upgrade_failed` and that the manager stderr it recorded contains both
   `invalid peer certificate` and `UnknownIssuer`, and prints the whole report
   and the whole error. Nothing is filtered: the certificate error is the point.
   This is the released 0.16.0, from before the retry, so it fails and stays
   failed.
3. **Proof 2, the upgrade under review heals itself.** A tool environment is
   provisioned by name and unpinned (`UV_SYSTEM_CERTS=1 uv tool install --force
   agentic-hil`) so that `uv tool upgrade` reaches the index and meets the same
   failure, and this checkout's package is overlaid onto it so the code that runs
   is the one under review. Then `agentic-hil upgrade --json` runs with
   `UV_SYSTEM_CERTS` removed from the environment, and the run reads the retry off
   the report: the first attempt carries the trust failure, the retry adds
   `UV_SYSTEM_CERTS=1`, and the result says that second attempt is the one that
   got through. This is the live check of the real CLI, uv, the child environment
   and the machine's own store together, the fix in `src/agentic_hil/upgrade.py`,
   not a stub. Because this checkout is newer than any release, the upgrade the
   retry completes is an already-current one; what it proves is that the retry
   reached the index with verification on and the command reported success.
4. **Proof 3, the anchor repairs it.** Proof 2 left this checkout overlaid on a
   force-installed latest release, so the bench is first returned to the seeded
   0.16.0, receipt and files alike, the version an operator from before the
   retry would still be on and the one the installer has a real upgrade to make
   from. Then the released one-line installer, fetched and run exactly as an
   operator would type it:
   `curl -LsSf https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.sh | sh`.
   The run asserts that the installer met the same certificate failure, said it
   was switching to this machine's own store, exited zero, and left the current
   release installed and answering `--version`. It is the way through for an
   operator still on a version from before the retry landed.

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
`tests/test_tls_proxy_eval.py::test_the_container_reproduces_every_proof_of_the_bench`,
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

#326 taught `agentic-hil upgrade` the same move the installer already makes:
recognise a trust failure and retry once against the machine's own store,
verification still on. That retry is pinned two ways. `tests/test_upgrade_certificates.py`
stubs the manager and runs everywhere without Docker; proof 2 here runs the real
thing on the bench, overlaying this checkout onto a real uv tool environment so
the retry meets real uv, a real child environment and the machine's own trust
store. The overlay is what lets an opt-in repository eval exercise the code under
review without waiting for a release to carry it: proof 2 does not install a
released artifact, it installs one by name only to reach the index the way proof
1 does, then replaces the code with this working tree's.

What is still keyed to a release is proof 1's seed. It is 0.16.0 on purpose, a
version from before the retry, so proof 1 reproduces the failure an operator on
that release still meets, and proof 3 shows the installer is their way through.
When the retry ships in a release old enough to seed a bench that then has a
newer one to move to, proof 2 can drop the overlay and seed that release
directly, and proof 1 can move its seed forward or retire.
