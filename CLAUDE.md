# Izzie

Személyes AI asszisztens. **Egy** felhasználó, magyar nyelv, otthoni x86 szerver.
Nem termék, nem többfelhasználós, nem kell skálázni. A hosszú távú cél egy
folyamatosan jelen lévő asszisztens hanggal és arccal — a mai döntéseket ez
alakítja, még ha a hang és az arc még nincs is kész.

## Architektúra

- **Brain** — Python / FastAPI, a szerveren fut. Ez a repo.
- **Kliens** — Tauri (Windows / Linux / Android), vékony, csak megjelenít. Még nem létezik.
- **LLM** — Google Gemini API (`google-genai`), modell: `gemini-3.6-flash`.
- **Adat** — SQLite. Az útvonalat az `IZZIE_DB_PATH` env var adja meg, az
  `IZZIE_PERSONA` mintájára. Fejlesztésben alapértelmezés a `data/izzie.db`
  a repóban (a `data/` gitignore-olva). Dockerben majd
  `~/docker/homelab-tools/appdata/izzie/db/`. Még nem létezik.

## Jelenlegi állapot

Kész:

- `POST /chat` — SSE stream, `app/main.py`
- `GET /health`
- Persona-réteg — `persona/izzie.yaml` + `app/persona.py`
- Mondathatár-felismerés a TTS-hez — `app/text.py`

Nincs kész: memória, óra/időkontextus, naptár, fájlkeresés, kliens, hang, Docker, tesztek.

A pontos állapotot a kód és a `git log` mondja meg, nem ez a szakasz. Ha eltérést
találsz a kód és az itt leírtak között, azt jelezd, ne csendben igazodj hozzá.

## Kötött szabályok

Ezek nem stílus kérdései. Mindegyik mögött van egy ok, és mindegyik azért van
leírva, hogy ne kelljen később újraírni. Ha valamelyiket meg akarod szegni,
előbb kérdezz.

1. **A `/chat` streamel.** Nincs szinkron változat mellette. A hang miatt kell,
   és két kódutat nem tartunk fenn ugyanarra.

2. **Az SSE event formátum kötött.** Ezek a típusok léteznek, más nem:
   - `{"type": "token",    "text": "..."}` — inkrementális, a UI-nak
   - `{"type": "sentence", "index": 0, "text": "..."}` — kész mondat, a TTS-nek
   - `{"type": "done"}`
   - `{"type": "error",    "message": "..."}`

   A `sentence` event azért van, hogy a TTS az első mondatot már mondhassa,
   míg a többi generálódik. Új mezőt hozzá lehet adni, meglévőt elvenni vagy
   átnevezni nem.

3. **A persona nem kerül vissza a kódba.** Minden, ami Izzie hangneméről,
   stílusáról, önmeghatározásáról szól, a `persona/izzie.yaml`-ba tartozik.
   A `app/persona.py` csak összerakja, nem tartalmaz tartalmat.

   Kivétel, ami nem kivétel: a `_LENGTH_HINT` és `_ADDRESS_HINT` sztringek a
   kódban maradnak. Ezek nem Izzie tartalma, hanem az enum-értékek fordítása
   prompt-utasításra — a yaml annyit mond, hogy `tegez`, a kód dolga tudni,
   hogy ez mit jelent. Ha ezek is kimennének, a yaml prompt-motorrá hízna.

4. **A system prompt egyetlen helyen áll össze:** `build_system_prompt()`.
   Futásidejű kontextust (idő, memória, naptár) kizárólag a `context` paraméteren
   keresztül lehet beadni. Ne fűzz promptot máshol össze.

5. **A `messages` tábla append-only.** A `content` mezőt soha semmi nem módosítja
   és soha semmi nem törli. Egyetlen kivétel a `redacted_at` flag beállítása,
   lásd `docs/memory.md`.

6. **A nyers napló az igazság forrása.** Minden más — összefoglaló, kivonatolt
   tény, később embedding — származtatott, tehát eldobható és újragenerálható.
   Ezért kap minden származtatott sor `extractor_version` mezőt.

7. **Titkok a `.env`-ben.** Ne olvasd ki, ne írasd ki logba, ne commitold,
   és ne tedd be példakódba. A `GEMINI_API_KEY` ott van.

8. **A `requirements.txt` csak közvetlen függőségeket tartalmaz,** rögzített
   verzióval. Tranzitív függőség nem kerül bele.

## Konvenciók

- Azonosítók, függvény- és változónevek angolul. Kommentek és docstringek magyarul.
- Időbélyeg mindig ISO 8601, UTC, szövegként tárolva.
- Új modul új fájl az `app/` alatt. A `main.py` maradjon vékony: endpointok
  és bekötés, üzleti logika nem.
- Ahol tiszta be- és kimenet van (pl. `split_sentences`, később a memória
  kiválogatása), oda pytest teszt kerül. Ahol LLM-hívás van, oda nem.

## Munkamódszer

- **Szeletekben dolgozunk.** A specek szeletekre vannak bontva. Egy feladat
  egy szelet. Ne építsd meg előre a következőt, még ha kézenfekvő is.
- **Terv előbb.** Nagyobb feladatnál előbb terv, jóváhagyás, aztán kód.
- **A spec frissítése a feladat része.** Ha munka közben egy döntés megváltozik,
  a `docs/` alatti spec módosítása ugyanabba a commitba tartozik. Elavult spec
  rosszabb, mint a semmi.

## Specek

- Memória modul: [docs/memory.md](docs/memory.md)

## Futtatás

```
cd ~/projects/Izzie
source venv/bin/activate
uvicorn app.main:app --reload
```
