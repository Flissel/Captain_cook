"""Repository-owned entrypoint; the verified loader injects the internal builder."""


def build_capability_factory(configuration, authorities):
    return _verified_internal_builder(configuration, authorities)
