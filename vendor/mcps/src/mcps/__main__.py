"""Allow ``python -m mcps`` as an alternative to the ``mcps`` console script."""

from .server import main

if __name__ == "__main__":
    main()
