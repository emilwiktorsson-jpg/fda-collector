#!/usr/bin/env python3
"""
pricetrack – steg 1 mot automatisk handel: mäter om fda-collectors signaler går att tjäna pengar på.

Läser data/events.csv (skrivs av collector.py) och följer varje GODKÄNT/CRL/STOPP-signal i kursen:
  p_before  = sista kurs före nyheten (SEC-mottagning / FDA-publicering)
  p_entry   = realistisk köpkurs: när vi upptäckt signalen + ENTRY_DELAY_MIN, tidigast när Avanza/Nordnet
              har öppet för aktien (USA: 10:00–22:30 sv, Sverige: 09:00–17:30 sv)
  därefter kurs 5/15/60 min efter entry, högsta/lägsta inom 60 min, dagens stängning och nästa dags stängning.

Nyckeltalet är "already_moved" = hur mycket kursen redan rört sig innan vi kunde köpa. Är den nära noll och
ret_60m/ret_close stor, finns en kant att automatisera. Resultat: data/signals.csv och data/signals-summary.md.

Kolumnen manual_ok fylls i för hand (1 = äkta besked, 0 = felsignal) så att träffsäkerheten kan mätas.
Inga order läggs. Inte investeringsråd.
"""
import csv
import datetime as dt
import os
import sys
import zoneinfo

DATA = os.environ.get("DATA_DIR", "data")
EVENTS = f"{DATA}/events.csv"
SIGNALS = f"{DATA}/signals.csv"
SUMMARY = f"{DATA}/signals-summary.md"
ENTRY_DELAY_MIN = int(os.environ.get("ENTRY_DELAY_MIN", "2"))
TRACK_CATS = ("GODKÄNT", "CRL", "STOPP")
GIVE_UP_DAYS = 8  # Yahoo har minutdata ca 30 dagar bakåt, men vi ger upp tidigare

UTC = dt.timezone.utc
ET = zoneinfo.ZoneInfo("America/New_York")
SE = zoneinfo.ZoneInfo("Europe/Stockholm")

# Listornas tickers -> Yahoo-symbol (svenska bolag på Stockholmsbörsen)
YAHOO = {"BIOA-B": "BIOA-B.ST", "CAMX": "CAMX.ST", "VICO": "VICO.ST", "EGTX": "EGTX.ST", "SOBI": "SOBI.ST",
         "IRLAB": "IRLAB-A.ST", "IRLAB-A": "IRLAB-A.ST", "ALZCUR": "ALZCUR.ST", "ALZ": "ALZ.ST"}

COLS = ["key", "ticker", "yahoo", "category", "watch", "source", "title", "url",
        "t_news_utc", "t_detect_utc", "lag_min", "t_entry_utc", "entry_note",
        "p_before", "p_entry", "already_moved_pct",
        "p_5m", "p_15m", "p_60m", "max_60m", "min_60m", "p_close_d0", "p_close_d1",
        "ret_5m_pct", "ret_15m_pct", "ret_60m_pct", "best_60m_pct", "worst_60m_pct", "ret_close_d0_pct", "ret_close_d1_pct",
        "status", "manual_ok", "updated_utc"]


def now_utc():
    return dt.datetime.now(UTC)


def parse_t(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        return None


def iso(t):
    return t.astimezone(UTC).isoformat(timespec="seconds") if t else ""


def pct(a, b):
    try:
        a, b = float(a), float(b)
        return f"{(a / b - 1) * 100:.2f}" if b else ""
    except (TypeError, ValueError):
        return ""


def is_swedish(ysym):
    return ysym.endswith(".ST")


def next_tradeable(t, swedish):
    """Första tidpunkt >= t då Emil kan handla aktien hos Avanza/Nordnet. USA-helgdagar hanteras inte."""
    lo, hi = ((9 * 60, 17 * 60 + 30) if swedish else (10 * 60, 22 * 60 + 30))
    s = t.astimezone(SE)
    for _ in range(10):
        if s.weekday() < 5:
            hm = s.hour * 60 + s.minute
            if hm < lo:
                return s.replace(hour=lo // 60, minute=lo % 60, second=0, microsecond=0).astimezone(UTC)
            if hm < hi:
                return s.astimezone(UTC)
        s = (s + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return None


# ----------------------------------------------------------------------------- kursdata
_cache = {}


def minute_bars(ysym, t0, t1):
    """1-minutersstaplar inkl. för-/efterhandel, index i UTC. None vid fel."""
    import yfinance as yf
    k = (ysym, t0.isoformat(timespec="minutes"), t1.isoformat(timespec="minutes"))
    if k in _cache:
        return _cache[k]
    try:
        h = yf.Ticker(ysym).history(start=t0, end=t1 + dt.timedelta(minutes=1),
                                    interval="1m", prepost=True, auto_adjust=False)
        if h is None or not len(h):
            h = None
        else:
            h = h[h.Close.notna()]
            h.index = h.index.tz_convert("UTC")
    except Exception as ex:  # noqa
        print(f"FEL minutdata {ysym}: {ex}", file=sys.stderr)
        h = None
    _cache[k] = h
    return h


def daily_closes(ysym, d0):
    import yfinance as yf
    try:
        h = yf.Ticker(ysym).history(start=d0 - dt.timedelta(days=1), end=d0 + dt.timedelta(days=8),
                                    interval="1d", auto_adjust=False)
        return [(i.date(), float(c)) for i, c in h.Close.dropna().items()]
    except Exception as ex:  # noqa
        print(f"FEL dagsdata {ysym}: {ex}", file=sys.stderr)
        return []


def price_at_or_before(h, t):
    s = h[h.index <= t]
    return float(s.Close.iloc[-1]) if len(s) else None


def price_at_or_after(h, t, max_wait_min=30):
    s = h[(h.index >= t) & (h.index <= t + dt.timedelta(minutes=max_wait_min))]
    return (float(s.Close.iloc[0]), s.index[0].to_pydatetime()) if len(s) else (None, None)


def measure(row):
    """Fyller i kursfält så långt data finns. Returnerar uppdaterad rad."""
    ysym = row["yahoo"]
    t_news, t_det = parse_t(row["t_news_utc"]), parse_t(row["t_detect_utc"])
    now = now_utc()
    if now - t_det > dt.timedelta(days=GIVE_UP_DAYS) and row["status"] != "klar":
        row["status"] = "klar" if row.get("p_entry") else "ingen data"
        return row
    swe = is_swedish(ysym)
    t_try = next_tradeable(t_det + dt.timedelta(minutes=ENTRY_DELAY_MIN), swe)
    if t_try is None or t_try > now:
        row["status"] = "väntar på öppning"
        return row
    # Yahoo ger max 8 dagars minutdata per anrop: hämta från dagen före nyheten till två dagar efter köpläget
    t0 = min(t_news, t_det) - dt.timedelta(days=1)
    t1 = min(now, max(t_try, t0 + dt.timedelta(days=1)) + dt.timedelta(days=2))
    if t1 - t0 > dt.timedelta(days=7):
        t0 = t1 - dt.timedelta(days=7)
    h = minute_bars(ysym, t0, t1)
    if h is None or not len(h):
        row["status"] = "väntar på data"
        return row

    pb = price_at_or_before(h, t_news)
    pe, te = price_at_or_after(h, t_try, max_wait_min=60 * 24 * 4)  # tunn handel: ta första stapeln efter
    if pe is None:
        row["status"] = "väntar på data"
        return row
    te = te.replace(tzinfo=UTC) if te.tzinfo is None else te.astimezone(UTC)
    row.update(t_entry_utc=iso(te), p_before=f"{pb:.4f}" if pb else "", p_entry=f"{pe:.4f}",
               already_moved_pct=pct(pe, pb) if pb else "")
    if te > t_try + dt.timedelta(minutes=5):
        row["entry_note"] = "ingen handel direkt – första avslut senare"
    elif t_try > t_det + dt.timedelta(minutes=ENTRY_DELAY_MIN + 1):
        row["entry_note"] = "upptäckt när marknaden var stängd – köp vid öppning"
    else:
        row["entry_note"] = "köp direkt"

    done = True
    for mins, col in ((5, "p_5m"), (15, "p_15m"), (60, "p_60m")):
        if now >= te + dt.timedelta(minutes=mins):
            p = price_at_or_before(h, te + dt.timedelta(minutes=mins))
            row[col] = f"{p:.4f}" if p else ""
        else:
            done = False
    win = h[(h.index >= te) & (h.index <= te + dt.timedelta(minutes=60))]
    if len(win) and now >= te + dt.timedelta(minutes=60):
        row["max_60m"], row["min_60m"] = f"{float(win.High.max()):.4f}", f"{float(win.Low.min()):.4f}"

    # Stängningskurser (ordinarie handel) för entry-dagen och nästa handelsdag
    d0 = te.astimezone(SE if swe else ET).date()
    closes = [c for c in daily_closes(ysym, d0) if c[0] >= d0]
    if closes and closes[0][0] == d0 and now.astimezone(SE if swe else ET).date() > d0:
        row["p_close_d0"] = f"{closes[0][1]:.4f}"
    else:
        done = False
    if len(closes) >= 2 and now.astimezone(SE if swe else ET).date() > closes[1][0]:
        row["p_close_d1"] = f"{closes[1][1]:.4f}"
    else:
        done = False

    for a, b in (("p_5m", "ret_5m_pct"), ("p_15m", "ret_15m_pct"), ("p_60m", "ret_60m_pct"),
                 ("max_60m", "best_60m_pct"), ("min_60m", "worst_60m_pct"),
                 ("p_close_d0", "ret_close_d0_pct"), ("p_close_d1", "ret_close_d1_pct")):
        row[b] = pct(row[a], pe) if row.get(a) else ""
    row["status"] = "klar" if done else "pågår"
    return row


# ----------------------------------------------------------------------------- in/ut
def load_signals():
    if not os.path.exists(SIGNALS):
        return {}
    with open(SIGNALS, encoding="utf-8") as f:
        return {r["key"]: {c: r.get(c, "") for c in COLS} for r in csv.DictReader(f)}


def save_signals(sig):
    rows = sorted(sig.values(), key=lambda r: r["t_detect_utc"])
    with open(SIGNALS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def new_signals(sig):
    """Nya GODKÄNT/CRL/STOPP i events.csv. Samma ticker+kategori samma dag (ET) räknas en gång (första)."""
    if not os.path.exists(EVENTS):
        return 0
    added = 0
    with open(EVENTS, encoding="utf-8") as f:
        for e in csv.DictReader(f):
            if e.get("category") not in TRACK_CATS:
                continue
            t_det = parse_t(e.get("ts_utc"))
            if not t_det:
                continue
            t_src = parse_t(e.get("source_time_utc"))
            t_news = min(t_src, t_det) if t_src else t_det
            for tk in [x for x in (e.get("ticker") or "").split("/") if x][:3]:
                tk = tk.strip().upper()
                key = f"{tk}|{e['category']}|{t_news.astimezone(ET).date()}"
                if key in sig:
                    continue
                r = {c: "" for c in COLS}
                r.update(key=key, ticker=tk, yahoo=YAHOO.get(tk, tk), category=e["category"], watch=e.get("watch", ""),
                         source=e.get("source", ""), title=(e.get("title") or "")[:140], url=e.get("url", ""),
                         t_news_utc=iso(t_news), t_detect_utc=iso(t_det),
                         lag_min=f"{(t_det - t_news).total_seconds() / 60:.0f}" if t_src else "", status="ny")
                sig[key] = r
                added += 1
    return added


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def write_summary(sig):
    rows = list(sig.values())

    def stats(rs, col):
        v = [fnum(r[col]) for r in rs if fnum(r[col]) is not None]
        if not v:
            return "–"
        v.sort()
        med = v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2
        pos = sum(1 for x in v if x > 0)
        return f"{sum(v) / len(v):+.1f} % snitt, {med:+.1f} % median, {pos}/{len(v)} positiva"

    def lag_stats(rs):
        v = sorted(fnum(r["lag_min"]) for r in rs if fnum(r["lag_min"]) is not None)
        return f"median {v[len(v) // 2]:.0f} min, max {v[-1]:.0f} min (SEC/FDA-tid -> upptäckt)" if v else "–"

    lines = [f"# Signalutvärdering (uppdaterad {now_utc().astimezone(SE):%Y-%m-%d %H:%M} sv)", "",
             f"Köp antas {ENTRY_DELAY_MIN} min efter att fda-collector upptäckt signalen, tidigast när Avanza/Nordnet "
             "har öppet. Avgifter och spread är inte medräknade. Inga order läggs.", ""]
    for label, sel in (("Alla signaler", lambda r: True),
                       ("Endast bekräftat äkta (manual_ok = 1)", lambda r: r["manual_ok"] == "1"),
                       ("Bevakade bolag (watch = 1)", lambda r: r["watch"] == "1")):
        for cat in TRACK_CATS:
            rs = [r for r in rows if r["category"] == cat and sel(r) and r["p_entry"]]
            if not rs:
                continue
            lines += [f"## {label} – {cat} ({len(rs)} st)", "",
                      f"- Upptäcktsfördröjning: {lag_stats(rs)}",
                      f"- Redan rört sig före köp: {stats(rs, 'already_moved_pct')}",
                      f"- 15 min efter köp: {stats(rs, 'ret_15m_pct')}",
                      f"- 60 min efter köp: {stats(rs, 'ret_60m_pct')}",
                      f"- Bästa inom 60 min: {stats(rs, 'best_60m_pct')}",
                      f"- Dagens stängning: {stats(rs, 'ret_close_d0_pct')}",
                      f"- Nästa dags stängning: {stats(rs, 'ret_close_d1_pct')}", ""]
    checked = [r for r in rows if r["manual_ok"] in ("0", "1")]
    if checked:
        ok = sum(1 for r in checked if r["manual_ok"] == "1")
        lines += [f"**Träffsäkerhet:** {ok}/{len(checked)} granskade signaler var äkta besked.", ""]
    lines += ["## Senaste 15", "", "| upptäckt (sv) | ticker | kat | fördröjn. | redan rört | +60 min | stängn. | status |",
              "|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["t_detect_utc"], reverse=True)[:15]:
        t = parse_t(r["t_detect_utc"])
        lines.append(f"| {t.astimezone(SE):%m-%d %H:%M} | {r['ticker']} | {r['category']} | {(r['lag_min'] + ' min') if r['lag_min'] else '–'} | "
                     f"{r['already_moved_pct'] or '–'} | {r['ret_60m_pct'] or '–'} | {r['ret_close_d0_pct'] or '–'} | "
                     f"{r['status']} |")
    with open(SUMMARY, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    os.makedirs(DATA, exist_ok=True)
    sig = load_signals()
    added = new_signals(sig)
    n = 0
    for k, r in sig.items():
        if r["status"] in ("klar", "ingen data"):
            continue
        try:
            sig[k] = measure(r)
            sig[k]["updated_utc"] = iso(now_utc())
            n += 1
        except Exception as ex:  # noqa – en trasig rad får inte stoppa resten
            print(f"FEL mätning {k}: {ex}", file=sys.stderr)
    save_signals(sig)
    write_summary(sig)
    print(f"pricetrack OK: {added} nya, {n} uppdaterade, {len(sig)} totalt")


if __name__ == "__main__":
    main()
