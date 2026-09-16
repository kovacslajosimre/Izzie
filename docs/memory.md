# Memória modul

Státusz: 1-2. szelet (séma, írás, visszakeresés és mag-profil) megvalósítva —
`app/db.py`, `app/memory.py`, bekötve az `app/main.py` `generate()`-jébe.
A 3-4. szelet (extractor, explicit parancsok) nem kezdődött el.
Kapcsolódó: [../CLAUDE.md](../CLAUDE.md)

## Mit old meg

Izzie emlékezzen arra, ami korábban elhangzott — nem csak az aktuális
beszélgetésen belül, hanem hetekkel, hónapokkal később is. Ez az a modul,
ami a különbséget adja egy chatbot és egy asszisztens között.

## Alapelv: nyers napló vs. származtatott réteg

Ez a modul legfontosabb döntése, és minden más ebből következik.

A **nyers napló** minden üzenet, ahogy elhangzott, időbélyeggel, append-only.
Ez az igazság forrása. Nem törlődik, nem módosul, nem jár le.

Minden más — session-összefoglalók, kivonatolt tények, később vektor-embeddingek
— ebből *származik*. Tehát bármikor eldobható és a naplóból újragenerálható.

Ennek a haszna akkor jelentkezik, amikor kiderül, hogy a tény-kivonatoló prompt
rossz volt: nem elveszett adat, hanem egy újrafuttatás. Ezért kap minden
származtatott sor `extractor_version` mezőt — hogy látszódjon, mi készült
melyik logikával.

## Séma

```sql
CREATE TABLE sessions (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,          -- ISO 8601, UTC
  ended_at      TEXT,
  closed_by     TEXT,                   -- 'timeout' | 'nightly' | 'manual'
  summary       TEXT,
  summary_model TEXT,
  summarized_at TEXT
);

CREATE TABLE messages (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id),
  role        TEXT NOT NULL,            -- 'user' | 'assistant'
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  status      TEXT NOT NULL,            -- 'complete' | 'partial'
  redacted_at TEXT                      -- lásd: Felejtés
);

CREATE TABLE facts (
  id                INTEGER PRIMARY KEY,
  kind              TEXT NOT NULL,      -- zárt lista, lásd lent
  content           TEXT NOT NULL,      -- egy mondat, természetes nyelven
  confidence        REAL,
  source_message_id INTEGER REFERENCES messages(id),
  created_at        TEXT NOT NULL,
  superseded_by     INTEGER REFERENCES facts(id),
  superseded_at     TEXT,
  deleted_at        TEXT,
  extractor_version TEXT NOT NULL,
  pinned            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_messages_session ON messages(session_id, created_at);
CREATE INDEX idx_facts_active     ON facts(kind)
  WHERE superseded_by IS NULL AND deleted_at IS NULL;
```

A `kind` **zárt lista**, nem szabad szöveg — különben az extractor huszonöt
kategóriát fog kitalálni:

`identity` · `preference` · `project` · `relationship` · `event`

## Mi kerül a promptba

Három különböző dolog, három különböző okból. Nem szabad összemosni őket.

1. **Mag-profil** (`pinned = 1`) — kb. 10-20 tény, ami *mindig* benne van.
   Ki a felhasználó, kik a közeli emberek, milyen gépei vannak, alap
   preferenciák. Ez nem keresés kérdése, ez a belépő. Kicsinek kell maradnia.

2. **Az aktuális beszélgetés utolsó N üzenete** — a folytonosság miatt.

3. **Az előző beszélgetés összefoglalója** — hogy a session határán se
   szakadjon meg a fonal (3. szelet).

4. **Releváns emlékek** — kiválogatva. Kezdetben egyszerűen: kulcsszó-egyezés
   plusz frissesség szerinti rangsor, `LIMIT 5`. Vektoros keresés később
   ráépíthető.

Interfész, ami már most ilyen legyen, hogy a vektoros verzió ne járjon
átírással:

```python
def core_profile() -> list[Fact]: ...
def retrieve(query: str, k: int = 5) -> list[Fact]: ...
```

A promptba kizárólag a `build_system_prompt(persona, context)` `context["memory"]`
mezőjén keresztül kerül be.

## Írás: ki hozza létre az emlékeket

**Nem** inline tool call chat közben. Megfontolt döntés: a modell szeszélyesen
hívná, és lassítaná a választ. Hangnál a késleltetés szent.

Két út van helyette:

1. **Háttér-extractor**, a session lezárása után. Külön LLM-hívás, megkapja a
   session naplóját, és strukturált tényeket ad vissza. Nem sürgős, nem látszik,
   konzisztens. Ez az alapeset.

2. **Explicit felhasználói parancs** („Izzie, jegyezd meg, hogy…"). Ez nem
   memóriakezelés, hanem UX: azonnal ír, és visszaigazolja.

### Mikor ér véget egy session

Chat ablaknál van „bezárás", egy mindig futó asszisztensnél nincs. Megoldás:

- **Inaktivitási időtúllépés:** ha az utolsó üzenet óta eltelt 30 perc,
  a session lezárul (`closed_by = 'timeout'`), és indul az extractor.
- **Háttérciklus:** az alkalmazáson belül, ötpercenként lezárja a
  lejárt sessionöket, és feldolgozza a lezártakat. Ez váltotta ki az
  eredetileg tervezett éjszakai jobot, lásd a 3. szelet döntéseit.

Ha ez nincs, vagy soha nem keletkezik emlék, vagy minden üzenet után lefut az
extractor és ég a kvóta.

## Amikor egy tény megváltozik

Nincs `UPDATE`. A régi sor marad, és kap egy `superseded_by` hivatkozást az
újra, plusz időbélyeget. Így megmarad, hogy mikor mi volt igaz, és téves
kivonatolás esetén vissza lehet állni.

Az extractor **ne próbáljon konzisztens azonosítókat kitalálni** — nem fog.
Ehelyett megkapja az adott `kind` aktív tényeit, és minden új állításnál
háromféleképpen dönthet:

- új tény
- kiváltja a `#id`-t
- nincs változás

## Felejtés: két különböző dolog

### Lejárat

`MEMORY_RETENTION_MONTHS`, `0` = örökre. **Jelenlegi döntés: `0`.** A nyers
napló nem jár le. Ha ez valaha megváltozik, a lejárat a naplóra vonatkozik,
a mag-profilra soha.

### Explicit felejtés

Ha a felhasználó azt kéri, hogy „felejtsd el, hogy X", nem elég a tényt
törölni: a legközelebbi újrafuttatásnál az extractor a naplóból visszahozza.

Ezért a törlés **két lépés**:

1. a tény `deleted_at` értéket kap
2. a forrásüzenetek `redacted_at` értéket kapnak

A redaktált üzenetek nem kerülnek be a promptba és nem kerülnek az extractor
bemenetébe. A `content` ettől még nem módosul — ez az egyetlen művelet, ami
a `messages` táblához hozzányúl.

## Szeletek

Egy feladat egy szelet. Ne épüljön előre a következő.

**1. Séma és írás.**
Táblák, migráció, minden üzenet naplózása a `/chat`-ben, session-kezelés a
30 perces időtúllépéssel. Se kivonatolás, se visszakeresés.
*Ellenőrizhető:* beszélgetsz, és a DB-ben ott vannak a sorok, helyes
session-csoportosítással.

**2. Visszakeresés és mag-profil.**
`core_profile()` és `retrieve()`, bekötve a `build_system_prompt` `context`
paraméterén. Tényeket egyelőre kézzel viszel be a DB-be.
*Ellenőrizhető:* tud olyat, amit nem ebben a beszélgetésben mondtál neki.

**3. Háttér-extractor és kiváltás.**
Az extractor-prompt, a `kind` besorolás, a supersede-logika, az éjszakai job.
*Ellenőrizhető:* egy beszélgetés után maguktól megjelennek értelmes tények.

**4. Explicit parancsok.**
„Jegyezd meg" és „felejtsd el", a redakcióval együtt.

## Megvalósítási döntések

### 1. szelet

Ezek az 1. szelet megkezdése előtt dőltek el, a kód átnézése nyomán.

**DB-hozzáférés.** Stdlib `sqlite3`, a blokkoló hívások `asyncio.to_thread`-del
kiszervezve. Nem `aiosqlite` — az is szálpoolt használ belül, tehát új
függőséget vennénk fel ugyanazért a viselkedésért. WAL mód bekapcsolva, hogy
az olvasás ne akadjon el írás közben.

**Migráció.** Nem külső eszköz, de nem is `CREATE TABLE IF NOT EXISTS`.
A SQLite `PRAGMA user_version` mezőjét használjuk: induláskor megnézzük,
hányas verziónál tart a séma, és sorban lefuttatjuk a hiányzó lépéseket.
Tíz sor kód, és amikor a séma változik, nem találgatás lesz.

**A `facts` tábla már az 1. szeletben létrejön,** üresen. Egy tábla
létrehozása ingyen van, viszont így egy migrációs lépés lesz kettő helyett.

**Session-hozzárendelés.** Nincs kliensoldali session azonosító, a
`ChatRequest` nem bővül. Minden `/chat` híváskor a szerver megnézi a nyitott
sessiont: ha annak utolsó üzenete 30 percnél régebbi, lezárja
(`ended_at`, `closed_by = 'timeout'`), és nyit egy újat. Lusta kiértékelés,
nem kell hozzá időzítő.

**Az 1. szeletben a lezárás csupasz könyvelés.** Extractor nincs, az a
3. szelet. A `closed_by = 'nightly'` és az éjszakai job szintén a 3. szelet —
amíg nincs mit kivonatolni, nincs mit futtatni, és a lusta lezárás miatt
nem marad árván nyitott session.

**`closed_by = 'manual'`** marad az enumban, de nincs hozzá endpoint.
Felkészülés egy későbbi explicit „beszélgetés lezárása" akcióra.

**DB-útvonal.** `IZZIE_DB_PATH` env var, az `IZZIE_PERSONA` mintájára.
Fejlesztésben alapértelmezés `data/izzie.db` a repóban, a `data/` gitignore-olva.
A szülőkönyvtárat az alkalmazás hozza létre, ha nem létezik.

**Megszakadt válasz naplózása.** Ha a generálás hibára fut, az addig
összegyűlt részleges válasz **bekerül** a `messages` táblába — elhangzott,
a nyers napló pedig az igazság forrása. De nem úgy, mintha befejeződött
volna: a `messages.status` mező `'complete'` vagy `'partial'`. A `partial`
üzenetek nem kerülnek az extractor bemenetébe.

### 2. szelet

Az 1. szelet lezárása után, a kód átnézése nyomán dőltek el.

**Előzmény: valódi többfordulós beszélgetés.** Az aktuális session üzenetei
a Gemini `contents` paraméterébe kerülnek, szerepkörökkel, nem szövegként a
system promptba. A modell a többfordulós formátumra van tanítva; a szövegként
beillesztett előzményt idézetként kezeli, nem beszélgetésként. Ezzel a
`generate()` már nem egyetlen `message` sztringet kap, hanem üzenetlistát.

**Az aktuális üzenet is a naplóból jön.** A `start_turn()` a generálás előtt
már beírja a user üzenetet, így az előzmény utolsó eleme maga az aktuális
üzenet. Nincs külön „előzmény + új üzenet" összefűzés: egy forrás, nem kettő.

**Vágás darabszám szerint.** `HISTORY_MESSAGE_LIMIT = 20` üzenet (nem forduló),
az `app/memory.py`-ban. Tokenalapú vágás később ráépíthető, ha egy hosszú
válasz túl sokat visz el.

**Mi kerül az előzménybe.** Csak az aktuális session, `created_at, id` szerint
rendezve, a redaktált sorok (`redacted_at IS NOT NULL`) nélkül. A `partial`
üzenetek bekerülnek: a felhasználó látta őket, a folytonossághoz hozzátartoznak.
Az extractor bemenetéből továbbra is kimaradnak — ez két különböző kérdés.

**Szerepkör-normalizálás.** A Gemini váltakozó szerepköröket vár, user-rel
kezdve. A napló ezt nem garantálja: üres válasz nem kerül naplóba, így két user
üzenet követheti egymást, a vágás pedig eshet egy assistant üzenet elé. Ezért a
konverzió:

- `assistant` → `model`
- az egymást követő azonos szerepkörű üzenetek egy fordulóba vonódnak össze,
  üres sorral elválasztva
- a lista eleji `model` fordulók eldobódnak

Ez tiszta függvény (pl. `to_gemini_contents()`). Gemini-specifikus, ezért a
`main.py`-ban él, amíg nincs külön LLM-réteg. A `memory.py` szolgáltatófüggetlen
marad: `{"role": ..., "content": ...}` listát ad vissza, `google.genai`-t nem
importál.

**`core_profile()`.** Aktív (`superseded_by IS NULL AND deleted_at IS NULL`),
`pinned = 1` tények, `kind`, azon belül `created_at` szerint. Nincs kemény
plafon, de 20 fölött warning a logba: a mag-profilnak kicsinek kell maradnia,
és jobb látni, mielőtt csendben elhízik.

**`retrieve(query, k=5)`: kulcsszó-egyezés Pythonban.** Betölti az aktív, nem
pinned tényeket, és Pythonban pontozza őket. Nem SQL `LIKE` és nem FTS5, mert:

- a magyar ragoz („szerver", „szerveren", „szerverről"), egész szavas egyezés
  nem működik;
- az FTS5 virtuális táblát, triggereket és migrációt hozna egy olyan rétegre,
  amit a vektoros keresés úgyis lecserél;
- néhány száz ténynél a Python-oldali pontozás ingyen van, és tiszta
  függvényként tesztelhető.

A pontozás:

- **Tokenizálás:** kisbetűsítés, szétvágás minden nem-betű és nem-szám
  karakternél, a kötőjelnél is („Izzie-ről" → `izzie`, `ről`). Az ékezetek
  maradnak, és nem csak a magyarok — a szóhatár Unicode-betű, nem egy fix
  magyar ékezet-lista, mert egy régi kódlapról érkező idegen ékezet
  („Straße", „kõnyvtár") is szót szakítana szét egy szűkebb mintánál; a
  számjegyek megtartása pedig gépnevekhez és verziószámokhoz kell
  („win11", „3060").
- **Eldobva:** a 3 karakternél rövidebb szavak és egy rövid magyar
  stopword-lista (`hogy`, `nem`, `van`, `egy`, `és`, `meg`, `mit`, `ami`,
  `azt`, `csak`, `már`, `még`, `volt`, `lesz`, `kell`, `nekem`, `neked` —
  bővíthető).
- **Egyezés:** két szó egyezik, ha a rövidebb a hosszabb prefixe, vagy a közös
  prefixük legalább 4 karakter. Szándékosan durva szótövezés: „gép" ~ „gépem",
  „kutyám" ~ „kutyáról". Lesz téves találat („szerver" ~ „szerda"), ezt
  elfogadjuk; a `k` limit tompítja.
- **Pontszám:** hány *különböző* query-szónak van egyezése a tényben. A 0
  pontos tény nem kerül vissza.
- **Rangsor:** pontszám szerint csökkenő, azonos pontnál `created_at` szerint
  csökkenő.

A query az aktuális user üzenet. A pinned tények kimaradnak, mert a mag-profil
már tartalmazza őket.

**A promptba kerülő blokk.** `format_memory(core, relevant) -> str` a
`memory.py`-ban, ennek a kimenete megy a `context["memory"]`-ba. Két rész,
soronként egy tény (`- ...`), „A felhasználóról:" és „Ami most releváns lehet:"
fejléccel. Az üres rész kimarad; ha mindkettő üres, a kimenet üres sztring, és
a `build_system_prompt` ilyenkor nem ír memória-blokkot. A fejlécek ugyanabba a
kategóriába esnek, mint a `_LENGTH_HINT`: prompt-szerkezet, nem persona-tartalom.
Ha Izzie kéretlenül sorolgatja az emlékeit, az viselkedési kérdés, és a javítás
a `persona/izzie.yaml`-ba megy, nem a kódba.

**Bekötés.** Egy async belépési pont a `memory.py`-ban (pl.
`load_turn_context(session_id, user_message)`), ami egyetlen `to_thread`-ben,
egyetlen kapcsolattal olvassa ki az előzményt, a mag-profilt és a találatokat.
A `generate()` ezt a `try` blokkon belül hívja, hogy DB-hiba esetén is `error`
event menjen ki. A `main.py`-ban csak bekötés marad: kontextus lekérése,
`build_system_prompt(persona, {"memory": ...})`, konverzió, stream.

**Kézi tények.** Amíg nincs extractor, a tényeket SQL-lel visszük be,
`extractor_version = 'manual'` értékkel. Így a 3. szelet meg tudja különböztetni
őket a kivonatoltaktól. (Védelmet ez nem jelent: a 3. szeletben az extractor
a kézi tényt is leválthatja, ha elavult.) Példa:

```sql
INSERT INTO facts (kind, content, created_at, extractor_version, pinned)
VALUES ('identity', 'A felhasználó neve Lajos.',
        strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), 'manual', 1);
```

**Ismert korlát, szándékosan.** Az előzmény a session határán megszakad:
30 perc csend után Izzie a korábbi beszélgetésből semmire nem emlékszik, csak a
tényekre. A 2. szeletben ez így helyes — az átívelést a 3. szelet adja. Az
előző session végét nem hozzuk át előre.

**Tesztek** (az 1. szelet mintájára: a lekérdező függvények kapcsolatot kapnak
paraméterként, a DB ideiglenes):

- előzmény: csak az aktuális session, sorrend, redaktált kimarad, `partial`
  bent marad, a limit a legutolsó N-et tartja meg
- `to_gemini_contents()`: szerepkör-leképezés, összevonás, eleji `model` eldobása
- `core_profile()`: superseded, törölt és nem pinned tény kimarad
- `retrieve()`: ragozott egyezés, stopword és rövid szó nem ad pontot, rangsor,
  pinned kimarad, 0 pont kimarad, `k` betartva
- `format_memory()`: üres bemenetre üres sztring, egyik rész üres

### 3. szelet

A 2. szelet élesítése után dőltek el.

**Háttérciklus az éjszakai job helyett.** Az alkalmazás indulásakor (FastAPI
lifespan) elindul egy asyncio taszk, ami `BACKGROUND_INTERVAL_SECONDS = 300`
másodpercenként két dolgot csinál, ebben a sorrendben:

1. lezárja a lejárt sessiont (`closed_by = 'timeout'`), **új session
   nyitása nélkül** — erre külön függvény kell, a `resolve_session()` nem
   használható, mert az újat is nyit;
2. feldolgozza a lezárt, még fel nem dolgozott sessionöket, egyesével.

Miért nem éjszakai job: a lusta lezárás miatt egy este 8-kor abbamaradt
beszélgetés csak a következő üzenetnél zárulna le, és a kivonatolás addig
várna. A `/chat`-be tenni pedig a válasz késleltetését rontaná. A ciklus egy
kódút, bármikor újrafuttatható, és nem kell hozzá cron vagy systemd timer,
ami a Dockerbe költözésnél külön gond lenne. Hátránya, hogy csak futó
uvicorn mellett dolgozik — ez itt adott.

A `closed_by = 'nightly'` érték így nem használt, de az enumban marad.
A taszk leállításkor (lifespan vége) rendben megszakad. Egy kör hibája
logolódik, de a ciklust nem állítja le.

**Türelmi idő.** Csak az a session kerül feldolgozásra, amelynek `ended_at`
értéke legalább `EXTRACT_GRACE_SECONDS = 120` másodperce van. Ritka
versenyhelyzet ellen: a ciklus lezárhat egy sessiont éppen akkor, amikor egy
`/chat` már hozzárendelte az üzenetét. A türelmi idő alatt a késve beírt
üzenet még bekerül a feldolgozásba.

**Migráció (séma v2).**

```sql
ALTER TABLE sessions ADD COLUMN extracted_at     TEXT;
ALTER TABLE sessions ADD COLUMN extract_attempts INTEGER NOT NULL DEFAULT 0;
```

Feldolgozatlan: `ended_at IS NOT NULL AND extracted_at IS NULL AND
extract_attempts < 3`.

**Egy LLM-hívás, két kimenet.** Sessionönként egyetlen hívás adja a tényeket
és a session-összefoglalót is. Az összefoglaló így nem kerül pluszba, és ez
oldja fel a 2. szelet ismert korlátját: a tények azt őrzik meg, ami *igaz* a
felhasználóról, az összefoglaló azt, *miről volt szó*.

**Bemenet.** A session `complete` és nem redaktált üzenetei, id-vel és
szerepkörrel, plusz az összes aktív tény id-vel és `kind`-dal. Ha a
sessionben nincs egyetlen user üzenet sem, LLM-hívás nélkül feldolgozottnak
jelöljük (`extracted_at` kitöltve, összefoglaló nélkül).

Az aktív tények egyetlen listában mennek, nem `kind` szerint szétválogatva: a
modell így akkor is megtalálja a leváltandót, ha más kategóriába sorolná.
Néhány száz ténynél ez belefér; ha nem fér, akkor jön a szűrés.

**Kimenet: strukturált JSON** (`response_mime_type = "application/json"` és
`response_schema`), nem szabad szöveg:

```json
{
  "summary": "2-4 mondat, magyarul, harmadik személyben",
  "facts": [
    {
      "action": "new | supersede",
      "kind": "identity | preference | project | relationship | event",
      "content": "egy mondat",
      "supersedes_id": 12,
      "source_message_id": 345
    }
  ]
}
```

A „nincs változás" nem tétel: amit a modell nem ír ki, az nem változik.

**Validálás.** Tételenként, a DB-írás előtt. Eldobjuk és logoljuk (nem hibát
dobunk), ha:

- a `kind` nincs a zárt listában;
- az `action` ismeretlen;
- `supersede`-nél a `supersedes_id` nem aktív tény, vagy ugyanebben a
  válaszban egy korábbi tétel már leváltotta;
- a `source_message_id` nem ehhez a sessionhöz tartozik (az `action`-től
  függetlenül);
- a `content` üres.

Ha maga a JSON értelmezhetetlen, az a kísérlet sikertelen.

**Leváltás.** Az új tény beszúródik, a régi `superseded_by` és
`superseded_at` értéket kap. Ez nem sérti a „nincs UPDATE" elvet: a régi
tény tartalma nem változik, csak a leváltás tényét rögzítjük.

- Az extractor **bármilyen** aktív tényt leválthat, a kézit és a pinned-et
  is. Egy elavult kézi tény különben ellentmondana az újnak.
- Az új tény **örökli** a leváltott `pinned` értékét.
- Új (`new`) tény **mindig** `pinned = 0`. Hogy mi kerül a mag-profilba, az a
  felhasználó döntése, nem az extractoré.

**Egy tranzakció.** Egy session összes új ténye, leváltása, az
összefoglaló (`summary`, `summary_model`, `summarized_at`) és az
`extracted_at` egyetlen tranzakcióban íródik. Félbeszakadt írás után az
újrafuttatás különben duplikálná a tényeket.

**Hibakezelés.** Sikertelen kísérlet (API-hiba, értelmezhetetlen JSON):
`extract_attempts` nő, és a következő körben újra próbálkozunk. Háromszor
bukott session után warning a logba, és kimarad — egy tartósan hibás session
ne égesse a kvótát. Újrafuttatni kézzel lehet, az `extract_attempts`
nullázásával.

**Az extractor-prompt fájlban él:** `prompts/extractor.md`, az
`IZZIE_EXTRACTOR_PROMPT` env var-ral felülírható, az `IZZIE_PERSONA`
mintájára. Induláskor betöltődik, hogy hiányzó fájlnál az uvicorn azonnal
elszálljon. A prompt a szabályokat tartalmazza; a bemenetet (üzenetek,
tények) a kód fűzi hozzá.

**Verziók és modell.** `EXTRACTOR_VERSION = "v1"`, a prompt minden érdemi
változásánál léptetni kell. `EXTRACTOR_MODEL` külön konstans, egyelőre
ugyanaz, mint a chat modellje — így később olcsóbb vagy helyi modellre
tehető a chat érintése nélkül. A `summary_model` ezt kapja. A `confidence`
`NULL` marad: az LLM saját magabiztosság-becslése nem megbízható, és semmi
nem használná.

**Tények formája.** Egy mondat, magyarul, harmadik személyben a
felhasználóról („A felhasználó…"). Az Izzie-hez szóló megfogalmazás („az
ötleted…") nem megengedett — a kézi tényekre is ez a szabály.

**Az összefoglaló a promptban.** A `load_turn_context()` a mag-profil és a
találatok mellé betölti a legutóbbi lezárt, összefoglalóval rendelkező
session összefoglalóját, ha az nem az aktuális session. A `format_memory()`
harmadik részként írja ki, a session befejezésének dátumával
(„Az előző beszélgetés (2026-09-16):"). Mindig csak egy: a régebbiek
tartalmát a tények hordozzák.

**Szerkezet.** Új modul: `app/extractor.py` (bemenet összerakása,
validálás, DB-írás, egy session feldolgozása, a ciklus egy köre). A Gemini
kliens paraméterként érkezik, nem az extractor hozza létre. A lifespan és a
taszk indítása a `main.py`-ban van, a logika nem.

**Tesztek.** LLM-hívásra nincs teszt; a hívás helyére kanonizált JSON-választ
adó hamis kliens kerül.

- lejárt session lezárása új nyitása nélkül; a friss session nyitva marad
- feldolgozatlan sessionök kiválasztása: türelmi idő, `extract_attempts`
  plafon, már feldolgozott kimarad
- bemenet: `partial` és redaktált kimarad, aktív tények id-vel
- validálás: minden eldobási eset külön
- leváltás: `superseded_by` beáll, a `pinned` öröklődik, `new` sosem pinned
- tranzakció: ha az írás közben hiba van, semmi nem marad a DB-ben
- hibás JSON és API-hiba: `extract_attempts` nő, `extracted_at` üres marad
- user üzenet nélküli session: nincs LLM-hívás, feldolgozottnak jelölve
- `load_turn_context()`: az előző összefoglaló bekerül, az aktuális
  sessioné nem
- migráció: v1-es DB v2-re áll, adatvesztés nélkül

**Füstpróba.** Beszélgetés valami új dologról, utána a session kézi
lezárása:

```sql
UPDATE sessions SET ended_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
                    closed_by = 'manual'
WHERE ended_at IS NULL;
```

Legfeljebb hét perc múlva (türelmi idő + ciklus) a `facts` táblában
megjelennek az új tények, a `sessions`-ben az összefoglaló. Utána egy új
beszélgetésben: „Miről beszélgettünk legutóbb?"

## Nyitott kérdések

- Kell-e a `retrieve()` query-jébe az előző egy-két user üzenet is (pl. „és az
  mennyi volt?" típusú visszautalásnál). A 2. szelet után, használat alapján.
- Hogyan lehet kényelmesen kezelni a tényeket (listázás, pinned ki/be, kézi
  javítás) SQL nélkül. Legkésőbb a kliensnél előjön.
