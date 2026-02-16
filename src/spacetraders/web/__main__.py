"""Entry point: python -m spacetraders.web → uvicorn on :8080."""

import uvicorn

from spacetraders.web.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "spacetraders.web.__main__:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
    )
