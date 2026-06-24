#!/usr/bin/env python3
"""Serve Alice Prometheus metrics over HTTP.

Prometheus cannot scrape a local file directly. This tiny exporter regenerates
Alice's metrics on each `/metrics` request and returns Prometheus text format.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from alice.observability import observability_artifacts


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/metrics", "/"}:
            self.send_response(404)
            self.end_headers()
            return
        try:
            observability_artifacts.generate_metrics_export()
            body = observability_artifacts.METRICS_PROM.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = f"alice_metrics_export_error 1\n# {type(e).__name__}: {e}\n".encode()
            self.send_response(500)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), MetricsHandler)
    print(f"alice metrics exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
