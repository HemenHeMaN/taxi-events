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
        
    # Fortuna-Spiele separat abrufen
    try:
        new["fortuna"] = fortuna()
    except Exception as e:
        print("fortuna Fehler:", e)

    events = []
    for venue in ["arena", "dome", "meh", "messe"]:
        # Alle normalen Arena-Events holen (ohne Fortuna, falls da doppelt)
        got = [e for e in new.get(venue, []) if not (venue == "arena" and "Fortuna" in e[2])]
        
        # Fortuna-Spiele zur Arena hinzufügen
        if venue == "arena":
            got += new.get("fortuna", [])
            
        if not new.get(venue) and venue != "arena":
            got = [e for e in old if e[3] == venue]
            
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