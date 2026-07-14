"""Entry point: `python -m zigbee_ninja` runs the collector."""

import os

import uvicorn

from .api.app import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("ZN_HOST", "0.0.0.0"),
        port=int(os.environ.get("ZN_PORT", "8686")),
    )


if __name__ == "__main__":
    main()
