from pathlib import Path
from pynext import create_app

app = create_app(Path(__file__).parent)
