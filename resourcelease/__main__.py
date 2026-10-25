"""Executable assembly: core remains independent of platform backends."""
from .backends.android import AndroidBackend
from .cli import main


def entrypoint():
    return main(backends={"android": AndroidBackend()})


if __name__ == "__main__":
    raise SystemExit(entrypoint())
