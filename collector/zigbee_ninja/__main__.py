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
        # The ninja-tap agent is a stdlib-only client that streams continuously
        # and does not answer WS control pings; disable server-initiated pings so
        # a live tap is not dropped every ping_timeout. Dead connections still
        # surface via TCP on the next send.
        ws_ping_interval=None,
    )


if __name__ == "__main__":
    main()
