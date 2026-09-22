"""Set resource limits in a fresh process, avoiding preexec_fn in threaded servers."""
from __future__ import annotations

import os
import resource
import sys


def main() -> None:
    cpu_seconds, file_bytes, address_bytes = (int(value) for value in sys.argv[1:4])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Virtual address budget; numerical libraries can otherwise reserve many thread stacks.
    resource.setrlimit(resource.RLIMIT_AS, (address_bytes, address_bytes))
    # Deno/V8 reserves large inaccessible address cages; writable allocations stay bounded.
    resource.setrlimit(resource.RLIMIT_DATA, (2 * 1024**3, 2 * 1024**3))
    os.environ.update(OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="2")
    os.execvp(sys.argv[4], sys.argv[4:])


if __name__ == "__main__":
    main()
