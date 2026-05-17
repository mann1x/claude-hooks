# DemoService

Hand-authored synthetic fixture for the tool_executor bench
`trivial-02-read-section` question. The Configuration section
below is what the oracle checks for content fidelity; reflowing
this file requires updating the oracle's expected line range.

## Overview

DemoService is a small HTTP service that proxies requests to a
backend cache and returns JSON. It exists only as a fixture for
the consultancy skill-eval bench — it is not a production tool.

## Installation

Install with pip from the local checkout:

    pip install -e .

The service requires Python 3.10 or newer.

## Configuration

DemoService reads three environment variables on startup:

- `DEMO_HOST` — bind address (default `127.0.0.1`).
- `DEMO_PORT` — TCP port (default `8080`).
- `DEMO_TIMEOUT_S` — upstream request timeout in seconds
  (default `30.0`). Set to `0` to disable the timeout
  entirely — this is not recommended for production.

Configuration values are validated at startup; an invalid value
causes the service to refuse to start with a non-zero exit code.

## Usage

Run with:

    demoservice --host 0.0.0.0 --port 9000

Send a request:

    curl http://localhost:9000/health

## Logging

Logs go to stderr at INFO level by default. Set the `DEMO_LOG`
environment variable to `DEBUG`, `WARNING`, or `ERROR` to
override. The format is fixed and is not configurable.

## License

MIT.
