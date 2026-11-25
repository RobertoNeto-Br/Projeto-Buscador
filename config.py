import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

try:
    CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "6"))
    if CHECK_INTERVAL_HOURS <= 0:
        CHECK_INTERVAL_HOURS = 6.0
except ValueError:
    CHECK_INTERVAL_HOURS = 6.0

try:
    MAX_LINKS = int(os.getenv("MAX_LINKS", "5"))
    if MAX_LINKS <= 0:
        MAX_LINKS = 5
except ValueError:
    MAX_LINKS = 5

ALERT_CHANNEL_ID_RAW = os.getenv("ALERT_CHANNEL_ID", "").strip()
ALERT_CHANNEL_ID = int(ALERT_CHANNEL_ID_RAW) if ALERT_CHANNEL_ID_RAW.isdigit() else None

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPEDO_TOKEN = os.getenv("SCRAPEDO_TOKEN", "").strip()

# Configurações do painel web local / API embutida
ENABLE_WEB_SERVER = os.getenv("ENABLE_WEB_SERVER", "true").strip().lower() not in ("0", "false", "no", "nao")
API_HOST = os.getenv("API_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
except ValueError:
    API_PORT = 8080

# Link público do seu painel na Vercel (ex: https://buscador-precos.vercel.app)
WEB_URL = os.getenv("WEB_URL", f"http://localhost:{API_PORT}").strip().rstrip("/")

# Opcional: senha/chave para proteger ações de escrita na API local
API_KEY = os.getenv("API_KEY", "").strip()

# Ponte em nuvem gratuita (Supabase) para conectar Discloud Gold + Vercel 24/7
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

# Controle de reciclagem de memória (sem perda de dados)
try:
    MAX_RAM_MB = float(os.getenv("MAX_RAM_MB", "155"))
except ValueError:
    MAX_RAM_MB = 155.0

try:
    RECYCLE_INTERVAL_MINUTES = max(1, int(os.getenv("RECYCLE_INTERVAL_MINUTES", "5")))
except ValueError:
    RECYCLE_INTERVAL_MINUTES = 5

DB_PATH = BASE_DIR / os.getenv("DB_PATH", "data/buscador.db")
WEB_DIR = BASE_DIR / "web"
