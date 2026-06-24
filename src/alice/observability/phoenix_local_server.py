#!/usr/bin/env python3
"""Run Phoenix with the gRPC collector disabled.

Phoenix 16.3.0 starts an OTLP/gRPC collector during app startup. On the operator's
current macOS runtime, grpc.aio cannot bind a local port, and that prevents the
HTTP UI plus OTLP/HTTP collector from starting. Alice exports spans over
OTLP/HTTP to ``/v1/traces``, so the gRPC listener is not needed for Alice.
"""
from __future__ import annotations

import grpc


_real_grpc_server = grpc.aio.server


class _NoBindGrpcServer:
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def add_insecure_port(self, address):
        return 1

    def add_secure_port(self, address, credentials):
        return 1

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

def _server(*args, **kwargs):
    return _NoBindGrpcServer(_real_grpc_server(*args, **kwargs))


grpc.aio.server = _server


if __name__ == "__main__":
    from phoenix.server.main import main

    main()
