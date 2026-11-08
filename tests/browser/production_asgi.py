"""Built example app for browser checks under an ASGI server."""
from pathlib import Path

from pynext import create_app

ROOT = Path(__file__).resolve().parents[2]
app = create_app(ROOT / "examples/basic", production=True)
