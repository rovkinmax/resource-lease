"""Executable assembly: core remains independent of platform backends."""
from .backends.android import AndroidBackend
from .backends.android_placement import AndroidPlacement
from .cli import main
from .namespace import default_guard_root


def entrypoint():
    guards = default_guard_root()
    android = AndroidBackend(placement=AndroidPlacement(guards))
    return main(backends={"android": android}, maintenance_backends={"android": android},
                guard_root=guards)


if __name__ == "__main__":
    raise SystemExit(entrypoint())
