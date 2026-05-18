import os

import uvicorn

from webhook_server import app

if __name__ == "__main__":
    host = os.getenv("ALPHA_STRIKE_HOST", "0.0.0.0")
    port = int(os.getenv("ALPHA_STRIKE_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, reload=False)
