#!/usr/bin/env python3
"""
fda-collector – bevakar FDA-besked för noterade bolag och skickar ntfy-notis.

Källor (alla officiella/gratis):
  1. SEC EDGAR, senaste 8-K-anmälningar (Atom). Noterade bolag måste rapportera väsentliga händelser
     (godkännande, CRL, clinical hold m.m.) i 8-K, ofta inom minuter–timmar efter pressmeddelandet.
     Tidsstämpeln är SEC:s mottagningstid – exakt och officiell.
  2. FDA:s pressmeddelanden (RSS) – större godkännanden som FDA själv kommunicerar.
  3. FDA Novel Drug Approvals (årslista, HTML) – fångar godkännanden som ännu inte gett 8-K.
  4. watchlist.csv – dina egna bevakade bolag/läkemedel/PDUFA-datum. Träff på bevakad ticker ger
     högsta prioritet, och du får en påminnelse dagen före PDUFA.

Endast bolag med ticker hos SEC (company_tickers.json) notifieras, dvs. noterade bolag.
Verktyget samlar data och notifierar. Det lägger inga order. Inte investeringsråd.

Miljövariabler: NTFY_TOPIC (notiser), SEC_USER_AGENT (krävs av SEC: "fda-collector din@epost").
"""
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import zoneinfo

import feedparser
import requests

DATA = os.environ.get("DATA_DIR", "data")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
UA = os.environ.get("SEC_USER_AGENT", "").strip() or "fda-collector (ange SEC_USER_AGENT med e-post)"
MAX_8K = int(os.environ.get("MAX_8K", "100"))

def sec_atom(form):
    return ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=" + form +
            f"&company=&dateb=&owner=include&start=0&count={MAX_8K}&output=atom")
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_EX = "https://www.sec.gov/files/company_tickers_exchange.json"
FDA_FEEDS = [f"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/{k}/rss.xml"
             for k in ("press-releases", "drugs", "biologics")]
FDA_NOVEL = f"https://www.fda.gov/drugs/novel-drug-approvals-fda/novel-drug-approvals-{dt.date.today().year}"

ET = zoneinfo.ZoneInfo("America/New_York")
SE = zoneinfo.ZoneInfo("Europe/Stockholm")

# --- Nyckelord. Ordningen avgör kategori (första träff vinner). Ändra inte lättvindigt. ---
CATEGORIES = [
    ("CRL", r"complete response letter|\bCRL\b|refus(e|al) to file|\bRTF\b"),
    # Riktiga besked formuleras aktivt: "announced today that the FDA has approved", "received FDA approval for".
    # Beskrivande boilerplate ("FDA-approved product", "potential approval") fångas INTE här (se NOT_NEWS).
    ("GODKÄNT", r"\b(FDA|Food and Drug Administration)(\s*\(\s*FDA\s*\))?\s+(has\s+|have\s+|today\s+)?(approved|cleared|granted\s+(accelerated\s+|full\s+|traditional\s+|marketing\s+|\d+\(k\)\s+|de novo\s+)?(approval|clearance|authorization))\b"
                r"|\breceived\s+(U\.?S\.?\s+)?(FDA\s+)?(approval|clearance|authorization)\b"
                r"|\b(FDA|Food and Drug Administration)\s+(approval|clearance|authorization)\s+(of|for)\b"
                r"|\bapprov(al|ed)\s+by\s+the\s+(U\.?S\.?\s+)?(FDA|Food and Drug Administration)\b"),
    ("STOPP", r"clinical hold|safety communication|black box|boxed warning"),
    ("ÖVRIGT", r"\bPDUFA\b|priority review|breakthrough therapy|fast track|advisory committee|\bAdCom\b|"
               r"orphan drug|label expansion|\bsBLA\b|\bsNDA\b|\bBLA\b|\bNDA\b|510\(k\)|de novo|premarket approval|\bPMA\b"),
]
FDA_WORD = re.compile(r"\bFDA\b|Food and Drug Administration", re.I)
SKIP_FDA_CONTEXT = re.compile(r"risk factors|forward-looking statements", re.I)  # endast för snippet-val
CITATION = re.compile(r"\bdoi:|\baccessed\b|\bet al\b|\bpublished (january|february|march|april|may|june|july|august|"
                      r"september|october|november|december)\b|\bretrieved\b", re.I)  # referenslistor = brus
HEAD_CHARS = 7000  # nyheten står i början av ett 8-K/pressmeddelande; referenser och boilerplate kommer sist
MONTHS = r"(january|february|march|april|may|june|july|august|september|october|november|december)"
# Utdrag som beskriver gamla/hypotetiska godkännanden, inte dagens besked
NOT_NEWS = re.compile(r"FDA-approved|FDA-cleared|potential(ly)? approv|previously (approved|cleared)|approved (by the FDA )?in 20\d\d|"
                      r"if approved|seek(s|ing)? (FDA )?approval|anticipated approval|subject to (FDA )?approval|"
                      r"(may|could|would|expects? to|plans? to) (be )?(receive|obtain|achieve|secure)|believes|"
                      r"the only FDA|first and only FDA|currently approved|is approved (in|for)|are approved (in|for)|"
                      r"not (yet )?(been )?(reviewed|cleared|approved|authorized)|(has|have|is|are) not\b|neither|"
                      # IND-godkännande = tillstånd att starta studie, inte produktgodkännande
                      r"\bIND\b|investigational new drug|as previously announced|"
                      # VD-/styrelsepresentationer som räknar upp gamla godkännanden
                      r"\b(led|oversaw|instrumental in|has taken|track record|career|joins?|joined|appoint(ed|ment)|welcome|"
                      r"brings|decades? of|years of experience)\b|"
                      # Förbehållstext ("no assurance ... will be approved by the FDA")
                      r"will be approved|(no|any) assurances?|forward-looking|cautionary|safe harbor|"
                      # Planer och möten, inte beslut ("plans to submit a BLA", "Type B meeting request")
                      r"(plans?|plann(ed|ing)|intends?|expects?|aims?|prepar(e|es|ing)) to (submit|file)|"
                      r"\bType [ABCD] meeting|meeting request|pre-?(BLA|NDA|MAA|IND)\b|pathway to|"
                      r"(traditional|accelerated) or (traditional|accelerated) approval|"
                      # Tillbakablick i kvartalsrapport ("In September, Abbott announced FDA approval")
                      r"\bin " + MONTHS + r"( \d{1,2})?(, 20\d\d)?,|"
                      r"\bquarter(ly)? (results|highlights)\b", re.I)
# Allt efter förbehållsrubriken är boilerplate – klipps bort innan klassning
BOILERPLATE_CUT = re.compile(r"forward[- ]looking statements|cautionary (note|statement)|safe harbor", re.I)
# "received approval for X from the FDA" – FDA måste stå direkt efter, utan annan myndighet emellan
FROM_FDA = re.compile(r"(?:(?!MHRA|\bEMA\b|European|Health Canada|PMDA|NMPA|CHMP|\.\s)[^\n]){0,60}?\b(from|by)\s+(the\s+)?(U\.?S\.?\s+)?(FDA|Food and Drug Administration)\b", re.I)
# FDA lägger ut gamla beslutssidor på nytt i RSS-flödet med dagens datum. Beslutsdatumet står i sidans text.
MAX_NEWS_AGE_DAYS = 7
DECISION_DATE = re.compile(r"\b(?:On|For Immediate Release:?)\s+" + MONTHS + r"\.?\s+(\d{1,2}),\s+(20\d\d)\b", re.I)
# Börser som räknas som noterade (handlas via Avanza/Nordnet). OTC-listade bolag notifieras bara om de är bevakade.
LISTED = ("NASDAQ", "NYSE", "CBOE", "BATS", "ARCA", "AMEX")
EXCHANGE = {}  # ticker -> börs enligt SEC ("Nasdaq", "NYSE", "OTC", ""); "?" = okänd

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})


# ----------------------------------------------------------------------------- hjälp
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def log_error(msg):
    os.makedirs(DATA, exist_ok=True)
    with open(f"{DATA}/errors.log", "a", encoding="utf-8") as f:
        f.write(f"{now_utc().isoformat(timespec='seconds')} {msg}\n")
    print("FEL:", msg, file=sys.stderr)


def get(url, timeout=30, retries=2):
    for i in range(retries + 1):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429, 503):
                time.sleep(2 + 3 * i)
                continue
            return r
        except requests.RequestException as e:
            if i == retries:
                raise
            time.sleep(2)
    return r


def load_state():
    p = f"{DATA}/seen.json"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"sec": [], "fda_press": [], "novel": [], "reminded": []}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    for k in st:
        st[k] = st[k][-5000:]
    with open(f"{DATA}/seen.json", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0)


def append_event(row):
    os.makedirs(DATA, exist_ok=True)
    p = f"{DATA}/events.csv"
    new = not os.path.exists(p)
    cols = ["ts_utc", "source", "ticker", "company", "category", "watch", "title", "url", "source_time_utc", "snippet"]
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in cols})


def heartbeat(stats):
    os.makedirs(DATA, exist_ok=True)
    p = f"{DATA}/heartbeat.csv"
    new = not os.path.exists(p)
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts_utc", "sec_entries", "sec_listed", "sec_fetched", "sec_hits", "fda_press_hits", "novel_hits", "notices"])
        w.writerow([now_utc().isoformat(timespec="seconds")] + [stats.get(k, 0) for k in
                                                                 ["sec_entries", "sec_listed", "sec_fetched", "sec_hits",
                                                                  "fda_press_hits", "novel_hits", "notices"]])


def fmt_times(t_utc):
    """'16:05 ET / 22:05 sv' från en tz-medveten tidpunkt."""
    if t_utc is None:
        return "tid okänd"
    return f"{t_utc.astimezone(ET):%H:%M} ET / {t_utc.astimezone(SE):%H:%M} sv ({t_utc.astimezone(SE):%Y-%m-%d})"


def notify(title, body, priority=3, tags="", click=""):
    """Skickar ntfy-notis. Titeln hålls ASCII (HTTP-huvud); svensk text i kroppen."""
    print(f"NOTIS [{priority}] {title}: {body[:160]}")
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC saknas – ingen notis skickad)")
        return
    for a, b in (("Å", "A"), ("Ä", "A"), ("Ö", "O"), ("å", "a"), ("ä", "a"), ("ö", "o"), ("–", "-")):
        title = title.replace(a, b)
    h = {"Title": title.encode("ascii", "ignore").decode(), "Priority": str(priority)}
    if tags:
        h["Tags"] = tags
    if click:
        h["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=h, timeout=20)
    except requests.RequestException as e:
        log_error(f"ntfy: {e}")


def strip_html(t):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t)


def classify(text):
    """Returnerar (kategori, snippet) eller (None, None). Kräver att FDA nämns."""
    if not FDA_WORD.search(text):
        return None, None
    c = BOILERPLATE_CUT.search(text, 300)
    if c:
        text = text[:c.start()]
        if not FDA_WORD.search(text):
            return None, None
    for cat, pat in CATEGORIES:
        for m in re.finditer(pat, text, re.I):
            a, b = max(0, m.start() - 140), min(len(text), m.end() + 180)
            snip = text[a:b].strip()
            if cat == "ÖVRIGT" and SKIP_FDA_CONTEXT.search(snip):
                continue  # NDA/BLA i riskfaktorer är brus; leta vidare
            if CITATION.search(snip):
                continue  # referens till någon annans godkännande
            if cat == "GODKÄNT":
                if NOT_NEWS.search(snip):
                    continue  # beskrivning av befintligt/hypotetiskt godkännande, inte dagens nyhet
                if not FDA_WORD.search(m.group(0)) and not FROM_FDA.match(text, m.end()):
                    continue  # "received approval" utan FDA – t.ex. MHRA/EMA
            return cat, snip
    return None, None


def is_listed(ticker):
    """Sant om tickern handlas på en riktig börs (eller om börsen är okänd)."""
    ex = EXCHANGE.get(ticker, "?")
    return ex == "?" or any(k in (ex or "").upper() for k in LISTED)


def fda_decision_age(e, text):
    """Dagar sedan FDA:s beslut enligt sidans egen text ("On December 1, 2023, the Food and Drug Administration ...").
    Flödets datum duger inte. None = inget datum hittat (då släpps träffen igenom)."""
    m = DECISION_DATE.search(text)
    if not m and e.get("link"):
        try:
            time.sleep(0.2)
            m = DECISION_DATE.search(strip_html(get(e["link"], timeout=20).text[:300_000]))
        except Exception as ex:  # noqa
            log_error(f"FDA sida {e.get('link')}: {ex}")
    if not m:
        return None
    try:
        d = dt.datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    except ValueError:
        return None
    return (dt.date.today() - d).days


# ----------------------------------------------------------------------------- watchlist
def load_watchlist():
    """watchlist.csv = bolag du följer (alla kategorier notifieras).
    pdufa-calendar.csv = kommande FDA-beslut i bred bemärkelse (påminnelse + GODKÄNT/CRL/STOPP, men inte ÖVRIGT)."""
    wl = {"tickers": {}, "drugs": [], "companies": [], "cal_only": set()}
    own = set()
    for t, r, is_cal in all_rows():
        if t not in wl["tickers"] or not is_cal:
            wl["tickers"][t] = r
        d = (r.get("drug") or "").strip()
        if len(d) >= 4:
            wl["drugs"].append((d.lower(), t))
        c = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (r.get("company") or "").lower())).strip()
        if len(c) >= 5 and (c, t) not in wl["companies"]:
            wl["companies"].append((c, t))
        (wl["cal_only"] if is_cal else own).add(t)
    wl["cal_only"] -= own  # bolag i watchlist.csv behåller full prioritet även om de står i kalendern
    return wl


def all_rows():
    """(ticker, rad, lägre_prioritet) för varje rad i watchlist.csv, pdufa-calendar.csv och x-watch.csv."""
    # x-watch.csv = case från bevakade X-profiler (t.ex. @jfais20); behandlas som kalendern (inga ÖVRIGT-notiser)
    for p, is_cal in (("watchlist.csv", False), ("pdufa-calendar.csv", True), ("x-watch.csv", True)):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    t = (r.get("ticker") or "").strip().upper()
                    if t and not t.startswith("#"):
                        yield t, r, is_cal


def pdufa_reminders(wl, st):
    today = dt.datetime.now(SE).date()
    for t, r, _ in all_rows():
        d = (r.get("pdufa_date") or "").strip()
        if not d:
            continue
        try:
            pd_ = dt.date.fromisoformat(d)
        except ValueError:
            continue
        for days, label in ((1, "imorgon"), (0, "idag")):
            key = f"{t}|{d}|{days}|{(r.get('drug') or '')[:20]}"
            if pd_ - today == dt.timedelta(days=days) and key not in st["reminded"]:
                notify(f"PDUFA {label}: {t}", f"{r.get('company', '')} – {r.get('drug', '')}. PDUFA-datum {d}. "
                                             f"{r.get('notes', '')}\n"
                                             f"Beskedet kan komma när som helst under dagen, ofta efter USA-stängning.",
                       priority=4, tags="calendar")
                st["reminded"].append(key)


# ----------------------------------------------------------------------------- berikning (för tradebedömning)
def market_status(now=None):
    """Handelsfönster i svensk tid för USA-aktier via Avanza/Nordnet (okt 2026):
    Nordnet förhandel 10:00–15:30, Avanza förhandel 11:00–15:30, ordinarie 15:30–22:00, Avanza efterhandel 22:00–22:30.
    USA-helgdagar hanteras inte."""
    now = now or dt.datetime.now(SE)
    hm = now.hour * 60 + now.minute
    wd = now.weekday()
    if wd >= 5:
        return "STÄNGD (helg). Nästa: förhandel mån 10:00 sv (Nordnet), 11:00 (Avanza)"
    if hm < 10 * 60:
        return "STÄNGD. Nästa: förhandel 10:00 sv (Nordnet), 11:00 (Avanza)"
    if hm < 11 * 60:
        return "FÖRHANDEL (Nordnet; Avanza från 11:00). Tunn likviditet, bred spread"
    if hm < 15 * 60 + 30:
        return "FÖRHANDEL (Avanza + Nordnet) till 15:30 sv. Tunn likviditet, bred spread"
    if hm < 22 * 60:
        left = 22 * 60 - hm
        return f"ORDINARIE HANDEL, {left // 60} h {left % 60:02d} min kvar till 22:00 sv"
    if hm < 22 * 60 + 30:
        return f"EFTERHANDEL (Avanza) till 22:30 sv – {22 * 60 + 30 - hm} min kvar. Order makuleras 22:30"
    nd = "mån" if wd == 4 else "imorgon"
    return f"STÄNGD. Nästa: förhandel {nd} 10:00 sv (Nordnet), 11:00 (Avanza)"


def quote_line(ticker):
    """Kurs, rörelse (inkl. för-/efterhandel) och börsvärde från Yahoo. Tom sträng om något fallerar."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        fi = t.fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close) if fi.previous_close else None
        mcap = float(fi.market_cap) if fi.market_cap else None
        cur = fi.currency or "USD"
        parts = [f"Kurs {last:.2f} {cur}"]
        if prev:
            parts.append(f"{(last / prev - 1) * 100:+.1f} % vs föregående stängning")
        try:
            h = t.history(period="1d", interval="1m", prepost=True)
            if len(h):
                ext = float(h.Close.dropna().iloc[-1])
                if abs(ext / last - 1) > 0.002:
                    parts.append(f"för-/efterhandel {ext:.2f} ({(ext / last - 1) * 100:+.1f} %)")
        except Exception:  # noqa
            pass
        if mcap:
            parts.append(f"börsvärde {mcap / 1e9:.2f} mdr {cur}" if mcap >= 1e9 else f"börsvärde {mcap / 1e6:.0f} M{cur}")
        return ", ".join(parts)
    except Exception as ex:  # noqa
        log_error(f"quote {ticker}: {ex}")
        return ""


def pdufa_context(ticker, drug_hint=""):
    """Var beslutet väntat? Jämför med pdufa_date i listorna."""
    today = dt.datetime.now(SE).date()
    best = None
    for t, r, _ in all_rows():
        if t != ticker.upper():
            continue
        d = (r.get("pdufa_date") or "").strip()
        if not d:
            continue
        try:
            pd_ = dt.date.fromisoformat(d)
        except ValueError:
            continue
        if best is None or abs((pd_ - today).days) < abs((best[0] - today).days):
            best = (pd_, r.get("drug") or "")
    if not best:
        return "PDUFA: inget datum i listorna – oväntat eller ej bevakat beslut"
    pd_, drug = best
    diff = (pd_ - today).days
    if diff == 0:
        when = "kom på PDUFA-dagen"
    elif diff > 0:
        when = f"kom {diff} dagar FÖRE PDUFA"
    else:
        when = f"kom {-diff} dagar efter PDUFA"
    return f"PDUFA: {pd_} ({drug}) i listorna – {when}"


# ----------------------------------------------------------------------------- SEC
def sec_tickers():
    """CIK -> (ticker, namn). Ett bolag kan ha flera värdepapper hos SEC (stamaktie, warrant, preferensaktie, rätt).
    Välj stamaktien: börsnoterad före OTC, sedan ticker utan bindestreck, sedan kortast, sedan SEC:s ordning."""
    cands = {}
    try:
        j = get(SEC_TICKERS_EX).json()
        f = j["fields"]
        i_c, i_n, i_t, i_e = f.index("cik"), f.index("name"), f.index("ticker"), f.index("exchange")
        for n, row in enumerate(j["data"]):
            cands.setdefault(int(row[i_c]), []).append((row[i_t], row[i_n], row[i_e] or "", n))
    except Exception as ex:  # noqa
        log_error(f"SEC tickers (exchange): {ex}")
        cands = {}
        r = get(SEC_TICKERS)
        r.raise_for_status()
        for n, v in enumerate(r.json().values()):
            cands.setdefault(int(v["cik_str"]), []).append((v["ticker"], v["title"], "?", n))
    out = {}
    for cik, lst in cands.items():
        for c in lst:
            EXCHANGE[c[0]] = c[2]
        t, name, _, _ = min(lst, key=lambda c: (not is_listed(c[0]), "-" in c[0], len(c[0]), c[3]))
        out[cik] = (t, name)
    return out


def sec_feed(st, tick, wl, stats, form="8-K", only_watch=False):
    """Läser SEC:s senaste anmälningar av given typ. 8-K = amerikanska bolag (alla noterade läses).
    6-K = utländska bolag (Novo Nordisk, Eisai m.fl.); stort flöde, så bara watchlist-bolag läses."""
    feed = feedparser.parse(get(sec_atom(form)).content)
    entries = feed.entries or []
    stats["sec_entries"] = stats.get("sec_entries", 0) + len(entries)
    hits = []
    for e in entries:
        eid = e.get("id") or e.get("link")
        if not eid or eid in st["sec"]:
            continue
        m = re.search(r"\((\d{5,10})\)", e.get("title", ""))
        cik = int(m.group(1)) if m else None
        if cik not in tick or (only_watch and tick[cik][0] not in wl["tickers"]):
            st["sec"].append(eid)
            continue
        stats["sec_listed"] = stats.get("sec_listed", 0) + 1
        ticker, company = tick[cik]
        accepted = None
        try:
            accepted = dt.datetime.fromisoformat(e.get("updated", "").replace("Z", "+00:00"))
        except ValueError:
            pass
        # Hämta anmälningens primära dokument
        try:
            idx = get(e.link, timeout=30)
            docs = re.findall(r'href="(/Archives/edgar/data/\d+/\d+/[^"]+?\.htm[l]?)"', idx.text)
            docs = [d for d in docs if "/xslF345X" not in d and not d.endswith("-index.htm")]
            text = ""
            for d in docs[:3]:
                time.sleep(0.15)
                rr = get("https://www.sec.gov" + d, timeout=30)
                text += " " + strip_html(rr.text[:400_000])[:HEAD_CHARS]
            stats["sec_fetched"] = stats.get("sec_fetched", 0) + 1
        except Exception as ex:  # noqa
            log_error(f"SEC dok {ticker}: {ex}")
            continue  # försök igen nästa körning (ej markerad som sedd)
        st["sec"].append(eid)
        cat, snip = classify(text)
        if not cat:
            continue
        watch = ticker in wl["tickers"]
        if not watch and not is_listed(ticker):
            stats["otc_skipped"] = stats.get("otc_skipped", 0) + 1
            continue  # OTC-listat och inte bevakat: går sällan att handla via Avanza/Nordnet
        stats["sec_hits"] = stats.get("sec_hits", 0) + 1
        hits.append(dict(source=f"SEC {form}", ticker=ticker, company=company, category=cat, watch=watch,
                         title=e.get("title", ""), url=e.link, t=accepted, snippet=snip))
        time.sleep(0.15)
    return hits


# ----------------------------------------------------------------------------- FDA
def company_index(tick):
    """Normaliserade bolagsnamn -> ticker, för matchning i FDA-texter."""
    junk = re.compile(r"\b(inc|corp|corporation|co|company|ltd|limited|plc|llc|lp|holdings?|group|pharmaceuticals?|"
                      r"therapeutics|biosciences?|biotherapeutics|biopharma|sa|nv|ag|se|ab)\b\.?", re.I)
    idx = {}
    for cik, (t, name) in tick.items():
        n = re.sub(r"[^a-z0-9 ]", " ", junk.sub(" ", name.lower()))
        n = re.sub(r"\s+", " ", n).strip()
        # Kräv två ord eller ett långt namn: "first", "apple", "target" ensamma ger falsklarm
        if (len(n.split()) >= 2 and len(n) >= 7) or len(n) >= 10:
            idx.setdefault(n, t)
    return idx


def match_companies(text, cidx, wl):
    low = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    low = re.sub(r"\s+", " ", low)
    found = []
    for n, t in cidx.items():
        if f" {n} " in f" {low} ":
            found.append(t)
    for d, t in wl["drugs"]:
        if d in text.lower() and t not in found:
            found.append(t)
    for c, t in wl["companies"]:  # bevakade bolag utan SEC-notering (t.ex. svenska)
        if f" {c} " in f" {low} " and t not in found:
            found.append(t)
    return found[:6]


def fda_press(st, tick, cidx, wl, stats):
    hits = []
    entries = []
    for url in FDA_FEEDS:
        try:
            entries += feedparser.parse(get(url).content).entries or []
        except Exception as ex:  # noqa
            log_error(f"FDA RSS {url}: {ex}")
    for e in entries[:120]:
        eid = e.get("id") or e.get("link")
        if not eid or eid in st["fda_press"]:
            continue
        st["fda_press"].append(eid)
        text = f"{e.get('title', '')}. {strip_html(e.get('summary', ''))}"
        cat, snip = classify("FDA " + text)
        if not cat:
            continue
        tks = [t for t in match_companies(text, cidx, wl) if t in wl["tickers"] or is_listed(t)]
        if not tks:
            continue  # inget noterat bolag identifierat
        age = fda_decision_age(e, text)
        if age is not None and age > MAX_NEWS_AGE_DAYS:
            stats["fda_old_skipped"] = stats.get("fda_old_skipped", 0) + 1
            continue  # gammalt beslut som FDA lagt ut på nytt i flödet
        t_utc = None
        try:
            t_utc = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
        except Exception:  # noqa
            pass
        stats["fda_press_hits"] = stats.get("fda_press_hits", 0) + 1
        hits.append(dict(source="FDA press", ticker="/".join(tks), company="", category=cat,
                         watch=any(t in wl["tickers"] for t in tks), title=e.get("title", ""), url=e.get("link", ""),
                         t=t_utc, snippet=snip))
    return hits


def fda_novel(st, cidx, wl, stats):
    hits = []
    try:
        r = get(FDA_NOVEL)
        if r.status_code != 200:
            return hits
    except Exception as ex:  # noqa
        log_error(f"FDA novel: {ex}")
        return hits
    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", r.text)
    # Kolumner: No., Drug Name, Active Ingredient, Approval Date, FDA-approved use. Ingen bolagskolumn,
    # så träff kräver att läkemedlet (namn eller substans) finns i watchlist.csv.
    for row in rows:
        cells = [strip_html(c).strip() for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
        if len(cells) < 5 or not re.search(r"\d{1,2}/\d{1,2}/\d{4}", cells[3]):
            continue
        key = f"{cells[1]}|{cells[3]}"
        if key in st["novel"]:
            continue
        st["novel"].append(key)
        text = " ".join(cells)
        tks = [t for d, t in wl["drugs"] if d in text.lower()]
        if not tks:
            continue
        stats["novel_hits"] = stats.get("novel_hits", 0) + 1
        hits.append(dict(source="FDA novel", ticker="/".join(dict.fromkeys(tks)), company="", category="GODKÄNT", watch=True,
                         title=f"{cells[1]} ({cells[2]}) godkänt {cells[3]} – {cells[4][:120]}", url=FDA_NOVEL, t=None,
                         snippet=text[:300]))
    return hits


# ----------------------------------------------------------------------------- main
def run_once():
    """En insamlingsrunda: hämta, klassa, notifiera, spara."""
    os.makedirs(DATA, exist_ok=True)
    st = load_state()
    wl = load_watchlist()
    stats = {}
    first_run = not st["sec"]

    try:
        tick = sec_tickers()
    except Exception as ex:  # noqa
        log_error(f"SEC tickers: {ex}")
        notify("fda-collector DATAFEL", f"Kunde inte hämta SEC:s tickerlista: {ex}", priority=4, tags="warning")
        heartbeat(stats)
        return
    cidx = company_index(tick)

    hits = []
    for form, only_watch in (("8-K", False), ("6-K", True)):
        try:
            hits += sec_feed(st, tick, wl, stats, form, only_watch)
        except Exception as ex:  # noqa
            log_error(f"SEC {form}: {ex}")
    hits += fda_press(st, tick, cidx, wl, stats)
    hits += fda_novel(st, cidx, wl, stats)
    pdufa_reminders(wl, st)

    # Sortera: bevakade först, sedan GODKÄNT/CRL före övrigt
    order = {"GODKÄNT": 0, "CRL": 0, "STOPP": 1, "ÖVRIGT": 2}
    hits.sort(key=lambda h: (not h["watch"], order.get(h["category"], 3)))
    for h in hits:
        append_event(dict(ts_utc=now_utc().isoformat(timespec="seconds"), source=h["source"], ticker=h["ticker"],
                          company=h["company"], category=h["category"], watch=int(h["watch"]), title=h["title"],
                          url=h["url"], source_time_utc=h["t"].isoformat(timespec="seconds") if h["t"] else "",
                          snippet=h["snippet"]))
        if first_run:
            continue  # första körningen: bygg bara upp "sedda" utan att spamma
        urgent = h["category"] in ("GODKÄNT", "CRL") or h["watch"]
        if h["category"] == "ÖVRIGT" and (not h["watch"] or h["ticker"].split("/")[0] in wl["cal_only"]):
            continue  # vardagsbrus (BLA/NDA-omnämnanden) notifieras bara för bolag i watchlist.csv
        title = f"{'BEVAKAD ' if h['watch'] else ''}{h['category']}: {h['ticker']}"
        t0 = h["ticker"].split("/")[0]
        lines = [f"{h['company'] or h['title']} – {h['category']}", f"{h['source']} {fmt_times(h['t'])}",
                 f"Marknad: {market_status()}"]
        if h["t"]:
            age_min = int((now_utc() - h["t"]).total_seconds() // 60)
            lines.append(f"Ålder: {age_min} min sedan publicering" + (" – SENT, kursen kan redan ha rört sig" if age_min > 60 else ""))
        q = quote_line(t0)
        if q:
            lines.append(q)
        lines.append(pdufa_context(t0))
        body = "\n".join(lines) + f"\n\n{h['snippet'][:400]}\n\nKontrollera själv i källan innan du agerar."
        notify(title, body, priority=5 if urgent else 3,
               tags="rotating_light" if h["category"] in ("GODKÄNT", "CRL") else "pill", click=h["url"])
        stats["notices"] = stats.get("notices", 0) + 1

    if first_run:
        notify("fda-collector igang", f"Första körningen klar. {len(st['sec'])} 8-K markerade som sedda, "
                                      f"{len(hits)} FDA-relaterade träffar loggade utan notis. Från nu notifieras nya.",
               priority=2, tags="white_check_mark")
    save_state(st)
    heartbeat(stats)
    print("OK", stats)


def main():
    """En körning, eller en slinga om LOOP_MINUTES > 0 (ny körning var POLL_SECONDS:e sekund tills tiden är slut)."""
    loop_min = float(os.environ.get("LOOP_MINUTES", "0") or 0)
    poll = max(60, int(os.environ.get("POLL_SECONDS", "180") or 180))
    end = time.time() + loop_min * 60
    while True:
        run_once()
        if time.time() + poll >= end:
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
