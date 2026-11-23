"""Settings, read from the environment (.env in development). No secrets live in code."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

APP_DB = Path(os.getenv("APP_DB", DATA_DIR / "ruclosed.db"))          # business data (multi-tenant)
MEMORY_DB = Path(os.getenv("MEMORY_DB", DATA_DIR / "memory.db"))      # LangGraph checkpointer + store

MODEL = os.getenv("MODEL", "claude-haiku-5-5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OFFLINE_MODE = os.getenv("OFFLINE_MODE", "0") == "1" or not ANTHROPIC_API_KEY

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-secret-change-me")
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "720"))

# Short-term memory policy (canvas box 8): last N messages go to the model, older ones are summarised.
KEEP_LAST_MESSAGES = int(os.getenv("KEEP_LAST_MESSAGES", "12"))
SUMMARISE_AFTER = int(os.getenv("SUMMARISE_AFTER", "16"))

# Business model (canvas box 3)
SUBSCRIPTION_PKR = 15000
PER_VISIT_PKR = 1000

FRONTEND_DIR = ROOT / "frontend"
