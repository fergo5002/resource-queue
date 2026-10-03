"""Source-checkout entry point, also used by cross-process tests."""
from resource_queue.resources import main

if __name__ == '__main__':
    raise SystemExit(main())
