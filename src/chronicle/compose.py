"""Forward explicit Docker Compose arguments with Chronicle's private configuration.

No shell, implicit service action, or configuration output is introduced. The
Compose files still choose the credentials supplied to each individual service.
"""

import os
import sys

from chronicle.db import configuration


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    try:
        environment = {**os.environ, **configuration()}
    except RuntimeError:
        print(
            "Chronicle configuration could not be loaded; Docker was not started.",
            file=sys.stderr,
        )
        return 2
    try:
        os.execvpe("docker", ["docker", "compose", *arguments], environment)
    except OSError:
        print("Docker Compose could not be started.", file=sys.stderr)
        return 127
    return 0  # execvpe replaces this process; reachable only with a test substitute.


if __name__ == "__main__":
    sys.exit(main())
