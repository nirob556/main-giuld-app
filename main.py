# -*- coding: utf-8 -*-
# main.py — root-level entrypoint, re-exports the real app so Railpack/any
# auto-detecting PaaS that looks for main.py/app.py in the project root finds
# something to run. Actual app lives in app/main.py.
from app.main import app  # noqa: F401

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 20333)), reload=False)
