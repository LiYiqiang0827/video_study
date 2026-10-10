from time import perf_counter

_STARTED = perf_counter()

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(process_started=_STARTED))
