import json
import re
import pathlib
import datetime as dt
import requests
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
DEG_URL = "https://www.deg-eishockey.de/saison/spielplan/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
TODAY = dt.datetime.now().date().isoformat()

JS_DETAIL = r"""() => {
    const body = document.body ? document.body.innerText : "";
    const clean = s => (s || "").replace(/\s+/g, " ").trim();

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
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(700)
        return page.evaluate(JS_DETAIL)
    except Exception as e:
        print("Detailseite Fehler:", href, e)
        return ""

def dlive(page, venue, url):
    page.goto(url, wait_until="networkidle", timeout=60000)

    try:
        page.wait_for_selector('a[href]', timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:
        pass

    for _ in range(40):
        btn = page.get_by_text("Mehr Events anzeigen")
        if btn.count() == 0 or not btn.first.is_visible():
            break
        try:
            btn.first.click(force=True)
            page.wait_for_timeout(1500)
        except Exception:
            break

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
        found[href] = (date, title)

    out = []
    print(venue, "-", len(found), "Termine gefunden")

    for href, (date, title) in found.items():
        uhrzeit = detail_time(page, href)
        out.append([date, "", title, venue, uhrzeit])

    return out

GERMAN_DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DAY_RE = r"(?:täglich|Mo|Di|Mi|Do|Fr|Sa|So)(?:\s*-\s*(?:Mo|Di|Mi|Do|Fr|Sa|So))?"
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s*-\s*(\d{2})\.(\d{2})\.(\d{4}))?")

def parse_opening_hours(lines):
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
        
        if not m and "Veranstaltungsort" in text:
            m_single = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            m_bis = re.search(r"bis\s+(\d{1,2})\.(\d{1,2})\.", text, re.IGNORECASE)
            if m_single:
                start = f"{m_single[3]}-{m_single[2]}-{m_single[1]}"
                if m_bis:
                    end_day = m_bis[1].zfill(2)
                    end_month = m_bis[2].zfill(2)
                    end_year = m_single[3]
                    end = f"{end_year}-{end_month}-{end_day}"
                else:
                    end = start
                
                name = lines[0] if lines else "Messe"
                if (name, start) in seen_names:
                    continue
                seen_names.add((name, start))
                
                out.append([start, end if end != start else "", name, "messe"])
                continue

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
            out.append([start, end if end != start else "", name, "messe"])
    return out

def messe(page):
    try:
        rows = parse_messe(requests.get(MESSE, headers=UA, timeout=30).text)
        if rows:
            return rows
    except Exception as e:
        print("messe requests Fehler:", e)
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
        note = (f"Anstoß: {g['time']} Uhr" if g['time'] else "Anstoß noch offen")
        out.append([g["date"], "", f"Fortuna – {g['opp']}", "arena", ("DFB-Pokal, " if pokal else "") + note])
    return out

def deg(page):
    try:
        page.goto(DEG_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print("DEG Seite laden Fehler:", e)
        return []

    soup = BeautifulSoup(page.content(), "html.parser")
    out = []
    seen = set()

    for el in soup.select("tr, li, .game, .match, [class*='spiel']"):
        text = el.get_text(" | ", strip=True)
        m_date = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if not m_date:
            continue
        date_iso = f"{m_date[3]}-{m_date[2]}-{m_date[1]}"

        is_home = False
        if (re.search(r"\bHeim\b|\bH\b", text, re.IGNORECASE) and not re.search(r"\bAuswärts\b|\bA\b", text, re.IGNORECASE)) or \
           ("PSD Bank Dome" in text or "PSD BANK DOME" in text):
            is_home = True

        if not is_home:
            continue

        m_time = re.search(r"(\d{2}:\d{2})\s*Uhr", text)
        time_str = m_time[1] if m_time else ""
        note = f"Beginn: {time_str} Uhr" if time_str else "Beginn noch offen"

        # Gegner aus dem Text extrahieren
        opponent = ""
        parts = [p.strip() for p in text.split("|") if p.strip()]
        for p in parts:
            if "DEG" not in p and "Düsseldorf" not in p and not re.search(r"\d", p) and len(p) > 2 and "Dome" not in p and "Heim" not in p:
                opponent = p
                break
        
        if not opponent:
            opponent = "Heimspiel"

        title = f"DEG - {opponent}"

        if (date_iso, time_str) in seen:
            continue
        seen.add((date_iso, time_str))

        out.append([date_iso, "", title, "dome", note])
        
    return out

def main():
    old = []
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8")).get("events", [])
        except Exception:
            old = []

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
        try:
            new["deg"] = deg(page)
        except Exception as e:
            print("deg Fehler:", e)
        browser.close()
    try:
        new["fortuna"] = fortuna()
    except Exception as e:
        print("fortuna Fehler:", e)

    events = []
    for venue in ["arena", "dome", "meh", "messe"]:
        # Im Dome alle Einträge herausfiltern, die "Duesseldorfer" oder "DEG" enthalten (damit D.LIVE sie nicht doppelt liefert)
        got = [e for e in new.get(venue, []) if not (venue == "arena" and "Fortuna" in e[2]) and not (venue == "dome" and ("Duesseldorfer" in e[2] or "DEG" in e[2]))]
        
        if venue == "arena":
            got += new.get("fortuna", [])
        if venue == "dome":
            got += new.get("deg", [])

        if not new.get(venue) and venue not in ["arena", "dome"]:
            got = [e for e in old if e[3] == venue]
            
        if venue == "arena" and not new.get("arena"):
            got += [e for e in old if e[3] == "arena" and not e[2].startswith("Fortuna")]
        if venue == "arena" and not new.get("fortuna"):
            got += [e for e in old if e[3] == "arena" and e[2].startswith("Fortuna")]

        if venue == "dome" and not new.get("dome"):
            got += [e for e in old if e[3] == "dome" and not ("Duesseldorfer" in e[2] or "DEG" in e[2])]
        if venue == "dome" and not new.get("deg"):
            got += [e for e in old if e[3] == "dome" and ("Duesseldorfer" in e[2] or "DEG" in e[2])]

        events += got
        
    extra = pathlib.Path("extra.json")
    if extra.exists():
        try:
            events += json.loads(extra.read_text(encoding="utf-8"))
        except Exception:
            pass
        
    events = [e for e in events if (e[1] or e[0]) >= TODAY]
    events = sorted({json.dumps(e, ensure_ascii=False) for e in events})
    events = sorted((json.loads(e) for e in events), key=lambda e: (e[0], e[2]))
    OUT.write_text(json.dumps({"updated": TODAY, "events": events}, ensure_ascii=False, indent=0), encoding="utf-8")
    print(len(events), "Events geschrieben")

if __name__ == "__main__":
    main()