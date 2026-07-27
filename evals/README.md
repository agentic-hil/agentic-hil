# Agentic HIL evaluations

This directory defines one versioned result contract with separate executors:

- [`install/`](install/) runs stochastic LLM installation evaluations in
  disposable Docker containers.
- [`hil/`](hil/) keeps real-hardware validation on an operator-controlled
  machine or VM.
- [`result.schema.json`](result.schema.json) is the common per-run result
  envelope.

Docker images and VM disks are build/runtime artifacts. They are not committed.
The test cases, runner, verifier, provisioning definition, and result contract
stay beside the product and change in the same pull request.

An installation result does not claim hardware success. A HIL result does not
claim that a model understood the installation guide. Release policy may require
both independent results.
