# src/config.py
from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

CLIENTS_CSV = DATA_DIR / "clientes.csv"

# OpenAI key (via env or .env)
OPENAI_API_KEY = config("OPENAI_API_KEY", default=None)
