# ICC Cloud Engine — frozen configuration. Version-stamped so any future
# tuning starts a NEW memory bucket instead of poisoning old evidence.
CONFIG_VERSION = "v1.0.0-github"

SYMBOLS = {
    "XAUUSD": {"yahoo": "XAUUSD=X", "name": "GOLD"},
    "GBPUSD": {"yahoo": "GBPUSD=X", "name": "GBPUSD"},
    "US30":   {"yahoo": "^DJI",     "name": "DOW/US30"},
    "NAS100": {"yahoo": "NQ=F",     "name": "NAS100"},
    "USOIL":  {"yahoo": "CL=F",     "name": "WTI OIL"},
    "BTC":    {"yahoo": "BTC-USD",  "name": "BITCOIN"},
}

# Correlation clusters: one open position per cluster slot
CLUSTERS = [
    ["US30", "NAS100", "BTC"],   # risk-on
    ["XAUUSD", "GBPUSD"],        # USD majors
    ["XAUUSD", "USOIL"],         # commodity/macro
]

TG_API = "https://api.telegram.org/bot{}/{}"

# --- Decision thresholds (DO NOT tune while learning) ---
MIN_MATCHES      = 5      # minimum for any history-based judgement
TAKE_MIN_MATCHES = 12     # evidence gate: TAKE needs this many, else capped RISKY
STRICT_SIM       = 65.0
MODERATE_SIM     = 55.0
STRUCT_SIM       = 45.0
RECENT_WEIGHT    = 1.30
TAKE_WR          = 57.0
SKIP_WR          = 50.0
MIN_EXP          = 0.0
RESCUE_QUALITY   = 72.0
REJECT_QUALITY   = 40.0
WEAK, SEVERE     = 45.0, 30.0
MIN_TP, MAX_TP   = 1.25, 3.5
SL_BUFFER_ATR    = 0.20
MIN_PROTECT_R    = 0.60
BE_OFFSET_R      = 0.05

REQUIRE_REGIME_MATCH  = True   # memory only matches same trend/range regime
REQUIRE_SESSION_MATCH = True   # memory only matches same session (LDN/NY/ASIA/OFF)

# --- Risk firewall ---
MAX_LOSS_STREAK = 3
PAUSE_SCANS     = 20
DAILY_LOSS_R    = -3.0

CADENCE_MIN   = 20      # must match the workflow cron
MAX_HISTORY   = 2000
COOLDOWN_H    = 4       # per symbol+direction alert cooldown
REPLAY_TTL_H  = 6       # raw replay resolves after this long
