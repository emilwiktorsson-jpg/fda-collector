# fda-collector – FDA-besked för noterade bolag, med notis i mobilen

Syster till insyn-collector och hype-collector. Bevakar officiella källor var 15:e minut och skickar en ntfy-notis när ett
noterat bolag får ett FDA-besked. **Verktyget lägger inga order och ger inga köpsignaler. Inte investeringsråd.**

## Källor och varför de är snabba
1. **SEC EDGAR, senaste 8-K.** Noterade bolag måste rapportera väsentliga händelser (godkännande, CRL, clinical hold)
   i en 8-K, oftast samma morgon eller kväll som pressmeddelandet. SEC:s mottagningstid är exakt och officiell och
   visas i notisen både i ET och svensk tid. Bara bolag med ticker hos SEC bevakas, dvs. noterade bolag.
2. **FDA:s egna flöden** (pressmeddelanden, läkemedel, biologiska läkemedel) för besked FDA själv kommunicerar.
3. **FDA Novel Drug Approvals** (årets lista) – notifierar när ett läkemedel från din watchlist dyker upp.
4. **watchlist.csv** – dina bevakade bolag. Träff på bevakad ticker ger högsta prioritet oavsett kategori, och
   ett ifyllt `pdufa_date` ger påminnelse dagen före och på dagen.

## Kategorier
| Kategori | Betyder | Notis |
|---|---|---|
| GODKÄNT | "FDA has approved / received FDA approval / cleared" | Alltid, prioritet hög |
| CRL | Complete Response Letter, refuse to file | Alltid, prioritet hög |
| STOPP | Clinical hold, boxed warning, safety communication | Alltid |
| ÖVRIGT | PDUFA, Fast Track, Breakthrough, AdCom, BLA/NDA-inlämning | Bara för bolag i watchlist |

Texten klassificeras på dokumentets inledning, där nyheten står. Beskrivande standardtext ("FDA-approved product",
"potential approval", referenslistor, negationer) filtreras bort. Det kommer ändå att slinka igenom falsklarm ibland:
**kontrollera alltid källan (länk i notisen) innan du agerar.**

## Uppsättning (10 min)
1. Installera appen **ntfy** (ntfy.sh) på mobilen och prenumerera på ett långt, slumpmässigt ämnesnamn
   (samma som för dina andra skrapor går bra).
2. Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `NTFY_TOPIC` = ditt ämnesnamn
   - `SEC_USER_AGENT` = `fda-collector din@epost.se` (SEC kräver kontaktuppgift i User-Agent, annars blockeras anropen)
3. Actions → collect → Run workflow. Första körningen markerar allt befintligt som "sett" och skickar en bekräftelse
   ("fda-collector igang") i stället för att spamma gamla besked. Därefter körs det automatiskt var 15:e minut vardagar.
4. Fyll i `watchlist.csv` (ticker, bolag, läkemedel, PDUFA-datum ÅÅÅÅ-MM-DD).

## Vad som sparas (data/, append-only)
- `events.csv` – varje träff: källa, ticker, kategori, bevakad, rubrik, länk, källans tidsstämpel, textutdrag.
- `seen.json` – redan behandlade anmälningar (ingen dubbelnotis).
- `heartbeat.csv` – körstatistik per körning. Slutar raderna komma har jobbet stannat.
- `errors.log` – fel.

## Kostnad
Ca 60 körningar per vardag à ~1 minut ≈ 1 300 Actions-minuter per månad. Gratisplanen för privata repon ger 2 000.
Kör insyn-collector var 5:e minut samtidigt kan taket nås; glesa då ut till `*/20` i `.github/workflows/collect.yml`.

## Kända begränsningar
- 8-K-flödet täcker bara bolag noterade i USA (inkl. ADR med SEC-rapportering). Svenska bolag utan SEC-notering
  (t.ex. BioArctic) syns bara via FDA:s egna flöden eller via partnerns 8-K (Eisai/Biogen).
- GitHub Actions-schemat kan försenas flera minuter vid hög last. Det är gratisplanens pris.
- Nyckelordsklassificering är en heuristik. Falsklarm och missar förekommer. Loggen i `events.csv` visar allt som
  flaggats, även det som inte notifierats, så att reglerna kan skärpas med facit i hand.
- Många besked kommer efter USA-stängning. Då går aktien inte att handla på Avanza/Nordnet förrän nästa dag, och
  gapet vid öppning har ofta redan prisat in nyheten. Värdet ligger i att veta direkt, inte i att alltid hinna före.
