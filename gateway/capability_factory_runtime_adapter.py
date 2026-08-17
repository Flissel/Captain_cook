"""Repository-owned entrypoint for the capability Factory runtime."""

from gateway.capability_factory_runtime_internal import build_capability_factory_runtime as _build_capability_factory_runtime


def build_capability_factory(configuration, authorities):
    return _build_capability_factory_runtime(configuration, authorities)
