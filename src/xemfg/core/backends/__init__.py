from .xemfg import BACKENDS, XeMFGBackend  # noqa: F401


def get_backend(name: str = "xemfg") -> XeMFGBackend:
    return BACKENDS[name]
