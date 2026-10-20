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

SEC_ATOM = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
            f"&start=0&count={MAX_8K}&output=atom")
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
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
                r"|\breceived\s+(U\.S\.\s+)?(FDA\s+)?(approval|clearance|authorization)\b"
                r"|\b(FDA|Food and Drug Administration)\s+(approval|clearance|authorization)\s+(of|for)\b"
                r"|\bapprov(al|ed)\s+by\s+the\s+(U\.S\.\s+)?(FDA|Food and Drug Administration)\b"),
    ("STOPP", r"clinical hold|safety communication|black box|boxed warning"),
    ("ÖVRIGT", r"\bPDUFA\b|priority review|breakthrough therapy|fast track|advisory committee|\bAdCom\b|"
               r"orphan drug|label expansion|\bsBLA\b|\bsNDA\b|\bBLA\b|\bNDA\b|510\(k\)|de novo|premarket approval|\bPMA\b"),
]
FDA_WORD = re.compile(r"\bFDA\b|Food and Drug Administration", re.I)
SKIP_FDA_CONTEXT = re.compile(r"risk factors|forward-looking statements", re.I)  # endast för snippet-val
CITATION = re.compile(r"\bdoi:|\baccessed\b|\bet al\b|\bpublished (january|february|march|april|may|june|july|august|"
                      r"september|october|november|december)\b|\bretrieved\b", re.I)  # referenslistor = brus
HEAD_CHARS = 7000  # nyheten står i början av ett 8-K/pressmeddelande; referenser och boilerplate kommer sist
# Utdrag som beskriver gamla/hypotetiska godkännanden, inte dagens besked
NOT_NEWS = re.compile(r"FDA-approved|FDA-cleared|potential(ly)? approv|previously (approved|cleared)|approved (by the FDA )?in 20\d\d|"
                      r"if approved|seek(s|ing)? (FDA )?approval|anticipated approval|subject to (FDA )?approval|"
                      r"(may|could|would|expects? to|plans? to) (be )?(receive|obtain|achieve|secure)|believes|"
                      r"the only FDA|first and only FDA|currently approved|is approved (in|for)|are approved (in|for)|"
                      r"not (yet )?(been )?(reviewed|cleared|approved|authorized)|(has|have|is|are) not\b|neither", re.I)

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
    for cat, pat in CATEGORIES:
        for m in re.finditer(pat, text, re.I):
            a, b = max(0, m.start() - 140), min(len(text), m.end() + 180)
            snip = text[a:b].strip()
            if cat == "ÖVRIGT" and SKIP_FDA_CONTEXT.search(snip):
                continue  # NDA/BLA i riskfaktorer är brus; leta vidare
            if CITATION.search(snip):
                continue  # referens till någon annans godkännande
            if cat == "GODKÄNT" and NOT_NEWS.search(snip):
                continue  # beskrivning av befintligt/hypotetiskt godkännande, inte dagens nyhet
            return cat, snip
    return None, None


# ----------------------------------------------------------------------------- watchlist
def load_watchlist():
    wl = {"tickers": {}, "drugs": []}
    p = "watchlist.csv"
    if not os.path.exists(p):
        return wl
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip().upper()
            if not t or t.startswith("#"):
                continue
            wl["tickers"][t] = r
            d = (r.get("drug") or "").strip()
            if len(d) >= 4:
                wl["drugs"].append((d.lower(), t))
    return wl


def pdufa_reminders(wl, st):
    today = dt.datetime.now(SE).date()
    for t, r in wl["tickers"].items():
        d = (r.get("pdufa_date") or "").strip()
        if not d:
            continue
        try:
            pd_ = dt.date.fromisoformat(d)
        except ValueError:
            continue
        for days, label in ((1, "imorgon"), (0, "idag")):
            key = f"{t}|{d}|{days}"
            if pd_ - today == dt.timedelta(days=days) and key not in st["reminded"]:
                notify(f"PDUFA {label}: {t}", f"{r.get('company', '')} – {r.get('drug', '')}. PDUFA-datum {d}. "
                                             f"Beskedet kan komma när som helst under dagen, ofta efter USA-stängning.",
                       priority=4, tags="calendar")
                st["reminded"].append(key)


# ----------------------------------------------------------------------------- SEC
def sec_tickers():
    r = get(SEC_TICKERS)
    r.raise_for_status()
    out = {}
    for v in r.json().values():
        out[int(v["cik_str"])] = (v["ticker"], v["title"])
    return out


def sec_8k(st, tick, wl, stats):
    feed = feedparser.parse(get(SEC_ATOM).content)
    entries = feed.entries or []
    stats["sec_entries"] = len(entries)
    hits = []
    for e in entries:
        eid = e.get("id") or e.get("link")
        if not eid or eid in st["sec"]:
            continue
        m = re.search(r"\((\d{5,10})\)", e.get("title", ""))
        cik = int(m.group(1)) if m else None
        if cik not in tick:
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
        stats["sec_hits"] = stats.get("sec_hits", 0) + 1
        hits.append(dict(source="SEC 8-K", ticker=ticker, company=company, category=cat, watch=watch,
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
        tks = match_companies(text, cidx, wl)
        if not tks:
            continue  # inget noterat bolag identifierat
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
def main():
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
    try:
        hits += sec_8k(st, tick, wl, stats)
    except Exception as ex:  # noqa
        log_error(f"SEC 8-K: {ex}")
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
        if h["category"] == "ÖVRIGT" and not h["watch"]:
            continue  # vardagsbrus (BLA/NDA-omnämnanden) notifieras bara för bevakade bolag
        title = f"{'BEVAKAD ' if h['watch'] else ''}{h['category']}: {h['ticker']}"
        body = (f"{h['company'] or h['title']}\n{h['source']} {fmt_times(h['t'])}\n\n{h['snippet'][:400]}\n\n"
                f"Kontrollera själv i källan innan du agerar.")
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


if __name__ == "__main__":
    main()
