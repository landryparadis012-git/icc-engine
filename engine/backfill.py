name: ICC Engine Backfill
on:
  workflow_dispatch: {}
permissions:
  contents: write
concurrency:
  group: icc-engine
  cancel-in-progress: false
jobs:
  backfill:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Run backfill
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python engine/backfill.py
      - name: Commit memory
        run: |
          git config user.name "icc-engine-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/
          if git diff --cached --quiet; then
            echo "no changes"
            exit 0
          fi
          git commit -m "historical backfill [skip ci]"
          for attempt in 1 2 3; do
            if git pull --rebase --autostash origin main; then
              if git push; then
                echo "memory pushed"
                exit 0
              fi
            else
              echo "rebase conflict — newest run's memory wins"
              git rebase --abort 2>/dev/null || true
              git fetch origin main
              git reset --soft origin/main
              git add data/
              git commit -m "historical backfill [skip ci]" || true
            fi
            echo "attempt $attempt failed — retrying"
            sleep $((attempt * 10))
          done
          echo "could not push this run"
          exit 1
        out.append(s / min(i + 1, n))
    return out


def rsi_series(closes, n=14):
    out = []
    for i in range(len(closes)):
        if i < n:
            out.append(50.0)
            continue
        g = l = 0.0
        for k in range(i - n + 1, i + 1):
            d = closes[k] - closes[k - 1]
            if d > 0:
                g += d
            else:
                l -= d
        out.append(100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n)))
    return out


def closed_index(bars, ts, dur):
    lo, hi, ans = 0, len(bars) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid][0] + dur <= ts:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def ensure_source_column(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(memory)").fetchall()]
    if "outcome_source" not in cols:
        con.execute("ALTER TABLE memory ADD COLUMN outcome_source TEXT")


def backfill_symbol(con, sym):
    cfg = config.SYMBOLS[sym]
    m15 = icc.fetch_chart(cfg["yahoo"], "15m", "60d")
    h1 = icc.fetch_chart(cfg["yahoo"], "60m", "3mo")
    d1 = icc.fetch_chart(cfg["yahoo"], "1d", "6mo")
    if len(m15) < WARMUP + TTL_BARS + 5 or len(h1) < 30 or len(d1) < 25:
        print(f"[backfill] {sym}: not enough history (m15={len(m15)} h1={len(h1)} d1={len(d1)})")
        return 0, 0, 0
    c15 = [b[4] for b in m15]
    e20s, e50s = ema_series(c15, 20), ema_series(c15, 50)
    atrs, rs15 = atr_series(m15), rsi_series(c15)
    hc, he20, he50 = [b[4] for b in h1], ema_series([b[4] for b in h1], 20), ema_series([b[4] for b in h1], 50)
    hr = rsi_series(hc)
    dc, de20, de50 = [b[4] for b in d1], ema_series([b[4] for b in d1], 20), ema_series([b[4] for b in d1], 50)
    dr = rsi_series(dc)
    lows = [b[3] for b in m15]
    highs = [b[2] for b in m15]
    wins = losses = 0
    last_event = {1: -999, -1: -999}

    for i in range(WARMUP, len(m15) - TTL_BARS):
        ts, o, h, l, c = m15[i]
        a = atrs[i]
        if a <= 0:
            continue
        e20, e50, r15 = e20s[i], e50s[i], rs15[i]
        bull15 = c > e20 > e50
        bear15 = c < e20 < e50
        if not (bull15 or bear15):
            continue
        swing_lo, swing_hi = min(lows[i - 33:i - 3]), max(highs[i - 33:i - 3])
        bodyq = min(100.0, abs(c - o) / max(h - l, 1e-9) * 100)
        dirn = 0
        if bull15 and r15 > 50 and ((l < swing_lo and c > swing_lo) or c > e20) and c > o and bodyq >= 35:
            dirn = 1
        elif bear15 and r15 < 50 and ((h > swing_hi and c < swing_hi) or c < e20) and c < o and bodyq >= 35:
            dirn = -1
        if dirn == 0 or i - last_event[dirn] < DEDUP_BARS:
            continue
        jh, jd = closed_index(h1, ts, 3600), closed_index(d1, ts, 86400)
        if jh < 25 or jd < 25:
            continue
        h1_ok = (hc[jh] > he20[jh] > he50[jh]) if dirn == 1 else (hc[jh] < he20[jh] < he50[jh])
        d_ok = (dc[jd] > de20[jd] > de50[jd]) if dirn == 1 else (dc[jd] < de20[jd] < de50[jd])
        conf = sum([d_ok, h1_ok, (hr[jh] > 50) if dirn == 1 else (hr[jh] < 50),
                    bull15 if dirn == 1 else bear15])

        entry = c
        sl = (swing_lo - a * config.SL_BUFFER_ATR) if dirn == 1 else (swing_hi + a * config.SL_BUFFER_ATR)
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + dirn * risk * config.TP_BASELINE_R

        # ---- simulate: baseline (SL-first) + full uncensored path ----
        baseline = None
        peak = trough = giveback = 0.0
        mfe_early = 0.0
        bars_peak = None
        for k in range(i + 1, i + 1 + TTL_BARS):
            fh, fl, fc = m15[k][2], m15[k][3], m15[k][4]
            r_hi = (fh - entry) / risk if dirn == 1 else (entry - fl) / risk
            r_lo = (fl - entry) / risk if dirn == 1 else (entry - fh) / risk
            peak = max(peak, r_hi, r_lo)
            trough = min(trough, r_lo, r_hi)
            if bars_peak is None and peak > 0.05:
                bars_peak = k - i
            if peak > 0.3:
                giveback = max(giveback, (peak - min(r_lo, r_hi)) / peak)
            if k - i <= EARLY_BARS:
                mfe_early = max(mfe_early, r_hi)
            sl_hit = fl <= sl if dirn == 1 else fh >= sl
            tp_hit = fh >= tp if dirn == 1 else fl <= tp
            if baseline is None:
                if sl_hit:
                    baseline = -1.0
                elif tp_hit:
                    baseline = config.TP_BASELINE_R
        if baseline is None:
            fc = m15[i + TTL_BARS][4]
            baseline = round(((fc - entry) / risk) if dirn == 1 else ((entry - fc) / risk), 3)

        rng = max(h - l, 1e-9)
        disp = min(5.0, abs(c - o) / a)
        dl = ((c - l) / rng * 100) if dirn == 1 else ((h - c) / rng * 100)
        mom = (30 if bull15 else 0) + (30 if h1_ok else 0) + dl / 100 * 20 + min(20.0, disp / 1.5 * 20)
        swept = (l < swing_lo and c > swing_lo) if dirn == 1 else (h > swing_hi and c < swing_hi)
        liq = 10 if swept else (6 if (c > e20 if dirn == 1 else c < e20) else 2)
        rsi_q = min(10.0, max(0.0, (r15 - 50) * 0.5)) if dirn == 1 else min(10.0, max(0.0, (50 - r15) * 0.5))
        quality = (20 + rsi_q + dl / 100 * 8 + bodyq / 100 * 7 + min(10.0, disp / 1.5 * 10)
                   + 5.0 + liq + conf / 4 * 25) * 0.75 + mom * 0.25

        cand = {"symbol": sym, "tf": "15m", "direction": dirn, "entry": entry, "sl": sl,
                "tp": config.TP_BASELINE_R,
                "feats": {"disp": disp, "cont": 1.0 if bull15 else 0.0,
                          "depth": min(5.0, (c - swing_lo) / a) if dirn == 1 else min(5.0, (swing_hi - c) / a),
                          "sweep": 1.0 if swept else 0.0, "vol": 50.0},
                "quality": quality, "momentum": mom,
                "regime": 1 if ((hc[jh] > he20[jh] > he50[jh]) or (hc[jh] < he20[jh] < he50[jh])) else 0,
                "session": icc.session_tag(ts)}
        path = {"peak_r": round(peak, 3), "failure_r": round(giveback, 3),
                "bars_peak": bars_peak, "bars_total": TTL_BARS, "mfe_early": round(mfe_early, 3)}
        icc.remember(con, cand, 2, "raw", baseline, round(peak, 3), round(trough, 3), int(ts), path)
        con.execute("UPDATE memory SET outcome_source='backfill' WHERE id=last_insert_rowid()")
        last_event[dirn] = i
        if baseline > 0:
            wins += 1
        else:
            losses += 1
    return wins, losses, wins + losses


def main():
    con = icc.db_connect()
    ensure_source_column(con)
    flag = con.execute("SELECT v FROM kv WHERE k='backfill_done'").fetchone()
    if flag:
        print(f"[backfill] already completed ({flag[0]}) — delete data/memory.db to re-run")
        return
    t0 = time.time()
    tw = tl = total = 0
    for sym in config.SYMBOLS:
        w, l, n = backfill_symbol(con, sym)
        print(f"[backfill] {sym}: {n} setups | W {w} / L {l}")
        tw += w
        tl += l
        total += n
    con.execute("INSERT INTO kv(k,v) VALUES('backfill_done',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),))
    con.commit()
    con.close()
    print(f"[backfill] DONE: {total} raw outcomes (W {tw} / L {tl}) in {time.time() - t0:.0f}s")
    if total:
        print(f"[backfill] baseline WR {tw / total * 100:.1f}% at fixed {config.TP_BASELINE_R}R — informational only")


if __name__ == "__main__":
    main()
