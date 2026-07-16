"""Convenience launcher for the HealthBoard API.

Equivalent to:  uvicorn app.main:app --reload
Override host/port via env if 8000 is busy/blocked, e.g. (PowerShell):
    $env:PORT=8080; .venv\\Scripts\\python main.py
"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "1") != "0"
    print(f"HealthBoard starting on http://{host}:{port}  (docs: /docs)")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
