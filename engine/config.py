# ICC Cloud Engine — frozen configuration. Version-stamped: any future tuning
# starts a NEW memory bucket instead of poisoning old evidence.
#
# NOTE (audit 2026-10): this file previously defined SL_BUFFER_ATR,
# MIN_PROTECT_R, BE_OFFSET_R, REQUIRE_*_MATCH, MAX_LOSS_STREAK, PAUSE_SCANS,
# DAILY_LOSS_R, CADENCE_MIN, MAX_HISTORY, COOLDOWN_H and REPLAY_TTL_H twice.
# Python silently kept the LAST copy, so the first block was dead code and two
# values were effectively wrong:
#   * CADENCE_MIN was 20 while engine.yml runs every 10 minutes, which doubled
#     the length of every loss-streak pause. Now 10 and covered by a test.
#   * MAX_HISTORY was effectively 2000 (not 8000). Kept at 2000 to preserve
#     the runtime behaviour actually in production; raise deliberately.
CONFIG_VERSION = "v1.1.1-github"  # bumped: single-source config (no behaviour change bar cadence fix)

SYMBOLS = {
    "XAUUSD": {"yahoo": "GC=F", "name": "GOLD"},  # XAUUSD=X delisted by Yahoo (404); GC=F = gold futures
    "GBPUSD": {"yahoo": "GBPUSD=X", "name": "GBPUSD"},
    "US30":   {"yahoo": "^DJI",     "name": "DOW/US30"},
    "NAS100": {"yahoo": "NQ=F",     "name": "NAS100"},
    "USOIL":  {"yahoo": "CL=F",     "name": "WTI OIL"},
    "BTC":    {"yahoo": "BTC-USD",  "name": "BITCOIN"},
}

CLUSTERS = [
    ["US30", "NAS100", "BTC"],
    ["XAUUSD", "GBPUSD"],
    ["XAUUSD", "USOIL"],
]

TG_API = "https://api.telegram.org/bot{}/{}"

# --- Decision thresholds (DO NOT tune while learning) ---
MIN_MATCHES      = 5
TAKE_MIN_MATCHES = 12
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
SL_BUFFER_ATR    = 0.20     # stop distance beyond the swing, in ATR
MIN_PROTECT_R    = 0.60     # fallback BE threshold
BE_OFFSET_R      = 0.05

REQUIRE_REGIME_MATCH  = True   # memory only matches same trend/range regime
REQUIRE_SESSION_MATCH = True   # memory only matches same session (LDN/NY/ASIA/OFF)

# --- Behavioural memory (history-driven management) ---
BEHAV_SOFT_MATCHES = 20   # raw matches: stats shown, not applied
BEHAV_FULL_MATCHES = 30   # raw matches: learned thresholds applied (blended)
TP_BASELINE_R      = 1.5  # raw baseline TP for backfill + live replays

# --- Risk firewall (see risk.py; every value is RISK_*-overridable per deployment) ---
MAX_LOSS_STREAK = 3       # consecutive losses that trigger a pause
PAUSE_SCANS     = 20      # scans to pause for (× CADENCE_MIN minutes)
DAILY_LOSS_R    = -3.0    # daily realised R that locks new setups for the day

# --- Scheduling / retention ---
CADENCE_MIN   = 10        # MUST match the engine.yml cron (*/10) — enforced by tests
MAX_HISTORY   = 2000      # memory rows retained per config version
COOLDOWN_H    = 4         # per symbol+direction alert cooldown
REPLAY_TTL_H  = 6         # raw replay resolves after this long
