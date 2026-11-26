"""Executable assembly: core remains independent of platform backends."""
import sys

from .android_cli import main as android_main
from .backends.android import AndroidBackend
from .backends.android_placement import AndroidPlacement
from .cli import main
from .namespace import default_guard_root


def entrypoint():
    if len(sys.argv) > 1 and sys.argv[1] == "android":
        return android_main(sys.argv[2:])
    guards = default_guard_root()
    android = AndroidBackend(placement=AndroidPlacement(guards))
    return main(backends={"android": android}, maintenance_backends={"android": android},
                guard_root=guards)


if __name__ == "__main__":
    raise SystemExit(entrypoint())
