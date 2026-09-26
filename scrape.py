"""Holt Events von D.LIVE (Arena, Dome, MEH), Messe Düsseldorf und Fortunas Heimspiele.
Schreibt events.json. Fällt eine Quelle aus, bleiben deren alte Einträge erhalten."""
import json, re, pathlib, datetime as dt, requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUT = pathlib.Path("events.json")
DLIVE = {
    "arena": "https://www.merkur-spiel-arena.de/events-tickets/eventkalender",
    "dome": "https://www.psd-bank-dome.de/events-tickets/eventkalender",
    "meh": "https://www.mitsubishi-electric-halle.de/events-tickets/eventkalender",
}
MESSE = "https://www.messe-duesseldorf.de/de/messen_und_events/messen_national_und_international"
FORTUNA = "https://www.fussballdaten.de/vereine/fortuna-duesseldorf/spielplan/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
TODAY = dt.datetime.now().date().isoformat()

# D.LIVE:
# Die Kalenderseite liefert Datum/Titel/Detail-Link.
# Die eigentliche Uhrzeit steht zuverlässig auf der jeweiligen Event-Detailseite.
# Deshalb öffnen wir jede Detailseite und lesen dort Einlass/Beginn/Ende aus.
JS_DETAIL = r"""() => {
    const body = document.body ? document.body.innerText : "";
    const clean = s => (s || "").replace(/\s+/g, " ").trim();

    // D.LIVE stellt die Werte als "Einlass: 18:30 Beginn: 20:00 Ende: 23:00"
    // dar. Wir suchen gezielt nach diesen Bezeichnungen.
    const get = label => {
        const re = new RegExp(label + "\\s*:?\\s*(\\d{1,2}[:.]\\d{2})", "i");
        const m = body.match(re);
        return m ? m[1].replace(".", ":") + " Uhr" : "";
    };

    const einlass = get("Einlass");
    const beginn  = get("Beginn");
    const ende    = get("Ende");

    const parts = [];
    if (einlass) parts.push("Einlass: " + einlass);
    if (beginn)  parts.push("Beginn: " + beginn);
    if (ende)    parts.push("Ende: " + ende);

    return parts.join(" | ");
}"""

JS_CALENDAR = r"""els => els.map(e => {
    let n = e;
    for (let i = 0; i < 7; i++) {
        n = n.parentElement;
        if (!n) break;
        const h = n.querySelector('h1,h2,h3,h4,h5');
        if (h && h.innerText.trim()) {
            return [e.href, h.innerText.trim()];
        }
    }
    return [e.href, ""];
})"""

def slug_title(slug):
    return re.sub(r"-\d{2}-\d{2}-\d{4}$", "", slug).replace("-", " ").title()

def detail_time(page, href):
    """Öffnet eine D.LIVE-Detailseite und liest Einlass/Beginn/Ende."""
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(700)
        return page.evaluate(JS_DETAIL)
    except Exception as e:
        print("Detailseite Fehler:", href, e)
        return ""

def dlive(page, venue, url):
    page.goto(url, wait_until="networkidle", timeout=60000)

    # Manche Kalender (z.B. MEH) laden die erste Event-Liste erst per
    # JavaScript nach dem eigentlichen Seitenaufbau. Kurz darauf warten,
    # bevor wir nach Datums-Links suchen.
    try:
        page.wait_for_selector('a[href]', timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:
        pass

    # Alle Events nachladen.
    for _ in range(40):
        btn = page.get_by_text("Mehr Events anzeigen")
        if btn.count() == 0 or not btn.first.is_visible():
            break
        try:
            btn.first.click(force=True)
            page.wait_for_timeout(1500)
        except Exception:
            break

    # Kalenderseite: nur Link, Titel und Datum erfassen.
    # Nicht nur Links mit "/event/" nehmen (MEH baut die Detail-Links
    # anders auf als Arena/Dome) - stattdessen alle Links prüfen und
    # anhand des Datums am Ende der Adresse erkennen (siehe unten).
    links = page.eval_on_selector_all('a[href]', JS_CALENDAR)

    found = {}
    for href, title in links:
        if not href:
            continue

        slug = href.rstrip("/").split("/")[-1]
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})$", slug)
        if not m:
            continue

        date = f"{m[3]}-{m[2]}-{m[1]}"
        title = title.strip() if title else slug_title(slug)

        # Gleiche URL kann mehrfach im Kalender vorkommen.
        found[href] = (date, title)

    out = []

    print(venue, "-", len(found), "Termine gefunden")

    # Jetzt jede Detailseite besuchen -> Uhrzeit holen.
    # Das ist absichtlich einzeln, damit Einlass/Beginn nicht mit einer
    # anderen Veranstaltung verwechselt werden.
    for href, (date, title) in found.items():
        uhrzeit = detail_time(page, href)
        out.append([date, "", title, venue, uhrzeit])

    return out

GERMAN_DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DAY_RE = r"(?:täglich|Mo|Di|Mi|Do|Fr|Sa|So)(?:\s*-\s*(?:Mo|Di|Mi|Do|Fr|Sa|So))?"
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s*-\s*(\d{2})\.(\d{2})\.(\d{4}))?")


def parse_opening_hours(lines):
    """Liest Zeilen wie 'Mi - Fr: 10:00 - 18:00' oder 'täglich: 09:00 - 18:00'
    und liefert ein Dict Wochentag -> (Start, Ende)."""
    hours = {}
    for line in lines:
        m = re.match(
            r"^(täglich|Mo|Di|Mi|Do|Fr|Sa|So)(?:\s*-\s*(Mo|Di|Mi|Do|Fr|Sa|So))?\s*:\s*"
            r"(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})$",
            line,
        )
        if not m:
            continue
        d1, d2, sh, sm, eh, em = m.groups()
        start, end = f"{int(sh):02d}:{sm}", f"{int(eh):02d}:{em}"
        if d1 == "täglich":
            for d in GERMAN_DAYS:
                hours[d] = (start, end)
        elif d2:
            i1, i2 = GERMAN_DAYS.index(d1), GERMAN_DAYS.index(d2)
            for i in range(i1, i2 + 1):
                hours[GERMAN_DAYS[i]] = (start, end)
        else:
            hours[d1] = (start, end)
    return hours


def expand_to_daily_rows(start_iso, end_iso, name, hours):
    """Erzeugt einen Eintrag pro Kalendertag zwischen Start und Ende,
    jeweils mit der für diesen Wochentag passenden Uhrzeit."""
    d0 = dt.date.fromisoformat(start_iso)
    d1 = dt.date.fromisoformat(end_iso) if end_iso else d0
    rows, cur = [], d0
    while cur <= d1:
        wd = GERMAN_DAYS[cur.weekday()]
        note = f"{hours[wd][0]}–{hours[wd][1]} Uhr" if wd in hours else ""
        row = [cur.isoformat(), "", name, "messe"]
        if note:
            row.append(note)
        rows.append(row)
        cur += dt.timedelta(days=1)
    return rows


def parse_messe(html):
    soup = BeautifulSoup(html, "html.parser")
    out, seen_names = [], set()
    for li in soup.find_all("li"):
        lines = [l.strip() for l in li.get_text("\n", strip=True).split("\n") if l.strip()]
        text = "\n".join(lines)
        m = DATE_RE.search(text)
        # Nur echte Veranstaltungskarten: haben einen Termin und "Veranstaltungsort"
        if not m or "Veranstaltungsort" not in text or not lines or DATE_RE.match(lines[0]):
            continue
        name = lines[0]
        start = f"{m[3]}-{m[2]}-{m[1]}"
        end = f"{m[6]}-{m[5]}-{m[4]}" if m[4] else start
        if (name, start) in seen_names:
            continue
        seen_names.add((name, start))

        hour_lines = []
        if "Öffnungszeiten:" in lines:
            for l in lines[lines.index("Öffnungszeiten:") + 1:]:
                if re.match("^" + DAY_RE + r"\s*:", l) or l == "täglich:":
                    hour_lines.append(l)
                elif l.endswith(":"):
                    break
        hours = parse_opening_hours(hour_lines)

        if hours:
            out.extend(expand_to_daily_rows(start, end, name, hours))
        else:
            # Keine Öffnungszeiten bekannt (z.B. reine Fachbesuchermessen):
            # ein Eintrag über den ganzen Zeitraum, wie bisher.
            out.append([start, end if end != start else "", name, "messe"])
    return out


def messe(page):
    """Holt die Messe-Termine. Die Seite liefert die Liste bereits im
    einfachen HTML mit, deshalb reicht ein normaler Abruf ohne Browser."""
    try:
        rows = parse_messe(requests.get(MESSE, headers=UA, timeout=30).text)
        if rows:
            return rows
    except Exception as e:
        print("messe requests Fehler:", e)
    # Rückfallebene: falls der einfache Abruf leer bleibt, per Browser laden.
    page.goto(MESSE, wait_until="networkidle")
    return parse_messe(page.content())


def fortuna():
    soup = BeautifulSoup(requests.get(FORTUNA, headers=UA, timeout=30).text, "html.parser")
    games = {}
    for a in soup.select("a[title]"):
        m = re.match(r"Fortuna Düsseldorf - (.+?) \| (\d{2})\.(\d{2})\.(\d{4}) \| (.+?) \|", a["title"])
        if not m:
            continue
        g = games.setdefault(a["href"], {"opp": m[1], "date": f"{m[4]}-{m[3]}-{m[2]}", "comp": m[5], "time": None})
        t = re.search(r"(\d{2}:\d{2})\s*Uhr", a.get_text())
        if t:
            g["time"] = t[1]
    out = []
    for g in games.values():
        pokal = "DFB" in g["comp"]
        note = (f"Anstoß: {g['time']} Uhr" if g["time"] else "Anstoß noch offen")
        out.append([g["date"], "", f"Fortuna – {g['opp']}", "arena", ("DFB-Pokal, " if pokal else "") + note])
    return out

def main():
    old = json.loads(OUT.read_text(encoding="utf-8"))["events"] if OUT.exists() else []
    new = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(locale="de-DE", user_agent=UA["User-Agent"]).new_page()
        for venue, url in DLIVE.items():
            try:
                new[venue] = dlive(page, venue, url)
            except Exception as e:
                print(venue, "Fehler:", e)
        try:
            new["messe"] = messe(page)
        except Exception as e:
            print("messe Fehler:", e)
        browser.close()
    try:
        new["fortuna"] = fortuna()
    except Exception as e:
        print("fortuna Fehler:", e)

    events = []
    for venue in ["arena", "dome", "meh", "messe"]:
        got = [e for e in new.get(venue, []) if not (venue == "arena" and "Fortuna" in e[2])]
        if venue == "arena":
            got += new.get("fortuna", [])
        if not new.get(venue) and venue != "arena":
            got = [e for e in old if e[3] == venue]
        if venue == "arena" and not new.get("arena"):
            got += [e for e in old if e[3] == "arena" and not e[2].startswith("Fortuna")]
        if venue == "arena" and not new.get("fortuna"):
            got += [e for e in old if e[3] == "arena" and e[2].startswith("Fortuna")]
        events += got
        
    extra = pathlib.Path("extra.json")
    if extra.exists():
        events += json.loads(extra.read_text(encoding="utf-8"))
        
    events = [e for e in events if (e[1] or e[0]) >= TODAY]
    events = sorted({json.dumps(e, ensure_ascii=False) for e in events})
    events = sorted((json.loads(e) for e in events), key=lambda e: (e[0], e[2]))
    OUT.write_text(json.dumps({"updated": TODAY, "events": events}, ensure_ascii=False, indent=0), encoding="utf-8")
    print(len(events), "Events geschrieben")

main()