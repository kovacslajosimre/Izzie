# Kliens

Állapot: a szerver-előkészítés, az 1. szelet (chatablak) és a 2. szelet
(várakozás és időkorlátok) kész és élesben tesztelt. A 3. szelet
(előzmény betöltése) szerver része (`GET /history`) kész, a kliens rész
következik. 2026 októberében a fejlesztés Ubuntura költözött, lásd
„Költözés Ubuntura”.

## Felállás

- **Kliens:** Tauri 2 + React + TypeScript, külön repóban
  (`kovacslajosimre/Izzie-client`). A laptopon a két repó egymás mellett
  él: `~/Documents/Project Izzie/Izzie` (ez a repó) és
  `~/Documents/Project Izzie/Izzie-Client`. A kliensből a spec tehát
  `../Izzie/docs/client.md`.
- **Identifier:** `com.kovacslajosimre.izzie`. Beleég a telepítőbe és az
  alkalmazás adatmappájának útvonalába — nem változtatjuk.
- **Hálózat:** a gépek Tailscale-en érik el egymást. A szerver címe
  `100.84.192.48`. A WireGuard titkosít, ezért a tailneten belül nem kell
  HTTPS.

## Szerver-előkészítés

Ami nélkül a kliens semmihez nem tud csatlakozni.

### Elérhetőség: csak a Tailscale-címen

Az uvicorn a Tailscale-címre köt (`--host 100.84.192.48`), nem a
`0.0.0.0`-ra. Így a tailneten kívülről (a helyi hálózatról sem) nem érhető
el, tűzfalszabály nélkül.

Következmények:

- A szerveren belüli `curl`-öknek is ezt a címet kell használniuk,
  `localhost` helyett.
- Ha az uvicorn a `tailscaled` előtt indul, a kötés elbukik. Kézi indításnál
  ez nem gond; a Docker-költözésnél újra kell gondolni (ott a konténer
  portját kötjük a Tailscale-címre).

Megfontolt alternatíva: `tailscale serve` (HTTPS-t és névfeloldást ad, az
uvicorn maradhat `127.0.0.1`-en). Egyelőre felesleges plusz réteg; ha a
kliensnek valaha HTTPS kell, ide térünk vissza.

### Bejelentkezés: bearer token

Nyitott port mellett bárki a tailneten (és bármi, ami egy tailnet-gépen
fut) a Gemini-kvótát égethetné és a memóriába írhatna. Ezért minden kérés
`Authorization: Bearer <token>` fejlécet hoz.

- A token az `IZZIE_API_TOKEN` env var, a `.env`-ben. Előállítása:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Hiányzó token esetén az alkalmazás nem indul el.** Nyitott végpontot
  csendben nem hagyunk. (A persona- és DB-ellenőrzés mintájára.)
- Az összehasonlítás `secrets.compare_digest`-tel történik, nem `==`-vel.
- Hibás vagy hiányzó fejléc: `401`, a token értéke nem kerül a logba.
- **FastAPI dependency, nem middleware.** A böngésző a CORS-előkérést
  (`OPTIONS`) fejléc nélkül küldi; ha a hitelesítés middleware lenne, azt is
  elutasítaná, és a kliens csak egy semmitmondó hálózati hibát látna.
- Minden végpont védett, kivéve egy esetleges egészség-ellenőrzőt
  (`/health`), ha van — az nem ad ki adatot.

### CORS

A Tauri ablaka böngészőmotor, és a böngésző nem engedi, hogy egy oldal más
címen lévő szerverrel beszéljen, hacsak a szerver ki nem mondja, hogy
elfogadja. Megengedett eredetek:

- `http://localhost:1420` — fejlesztés közben (Vite)
- `tauri://localhost` — a lefordított alkalmazás Linuxon (és macOS-en)
- `http://tauri.localhost` — a lefordított alkalmazás Windowson és
  Androidon

Az `IZZIE_CORS_ORIGINS` env var-ral felülírható (vesszővel elválasztva).
Metódusok: `GET`, `POST`. Fejlécek: `Authorization`, `Content-Type`.
Sütit nem használunk (`allow_credentials = False`).

A `CORSMiddleware` a `401`-es válaszra is rárakja a fejléceket, így a
kliens a hibás tokent `401`-ként látja, nem hálózati hibaként.

### Mellékesen: az AFC-figyelmeztetés

A `google-genai` minden hívásnál figyelmeztet az automatikus
függvényhívásra. Eszközhívást nem használunk, ezért a chat és az extractor
hívásában is kikapcsoljuk
(`automatic_function_calling = AutomaticFunctionCallingConfig(disable=True)`).

### Füstpróba

A szerveren:

```bash
curl -i http://100.84.192.48:8000/chat          # 401 vagy 405, nem 200
curl -N -X POST http://100.84.192.48:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" --data-binary @/tmp/msg.json
```

A laptopról ugyanez `curl.exe`-vel, és egy hibás tokennel is: `401`.
A helyi hálózati címről (`192.168.1.57:8000`) a kapcsolatnak el kell
bukni.

Eredmény (2026-09-16): minden eset a várt módon működött, a szerveren és a
laptopról is.

### Indítás

```bash
uvicorn app.main:app --reload --host 100.84.192.48
```

tmuxban (`tmux new -s izzie`), hogy egy SSH-szakadás ne állítsa le. A
`--reload` a `.env` változását nem veszi észre: token-csere után kézi
újraindítás kell.

### Ismert buktató: VPN a laptopon

(A ProtonVPN azóta nincs használatban; a tanulság más VPN-re is érvényes,
lásd „Költözés Ubuntura”.)

Bekapcsolt ProtonVPN mellett a Tailscale-címekre menő forgalom a laptopon
elakad: a kapcsolat 1-2 ms alatt elutasításra kerül, miközben a
`tailscale ping` működik (az a Tailscale saját csatornáján megy). Az
alkalmazás-alapú kivétel (a Tailscale app felvétele) nem elég, mert a
kérést nem a Tailscale küldi, hanem a `curl` vagy a kliens. A megoldás a
ProtonVPN split tunnelingjében a `100.64.0.0/10` tartomány kizárása.

## 1. szelet: chatablak

Cél: egy egyszerű, de valódi chatablak. Beírt üzenet, Izzie válasza
szavanként megjelenik, ahogy a szerver küldi. Semmi más.

### Beállítások

- `VITE_IZZIE_URL` (pl. `http://100.84.192.48:8000`) és `VITE_IZZIE_TOKEN`
  egy `.env.local` fájlban a kliens repó gyökerében. A `*.local` minta már a
  `.gitignore`-ban van, a git nem viszi fel.
- Legyen egy `.env.example` a két változóval, érték nélkül.
- Ha bármelyik hiányzik, a felület ezt jelzi egy érthető hibaüzenettel,
  kérés helyett.
- **Ismert korlát, szándékosan:** a `VITE_` változók build-kor beleégnek a
  JavaScriptbe. Fejlesztéshez ez elfogadható, kiadott alkalmazáshoz nem. Az
  első `tauri build` előtt a token az operációs rendszer jelszótárába
  költözik (Linuxon a Secret Service, pl. GNOME Keyring; külön szelet).

### Hálózat: a webview `fetch`-je

A kérés a webview beépített `fetch`-jével megy, nem a Tauri HTTP
pluginjével és nem Rust-parancson át. A szerver CORS-a ezt már kezeli, és így
a streamelés a böngésző saját eszközeivel megoldható. Rust-kódhoz ebben a
szeletben nem nyúlunk, csak a sablon `greet` parancsát és a hozzá tartozó
kódot takarítjuk ki.

### Streamelés

Az `EventSource` csak GET-et tud, a `/chat` POST. Ezért a kliens a
`response.body` folyamot maga olvassa és bontja.

- **A bontás tiszta függvény, külön modulban** (pl. `src/sse.ts`), ami a
  beérkező darabokból eseményeket ad. Nem függ a Reacttől és a hálózattól.
- A darabhatár bárhol lehet: egy sor közepén, két esemény között, és egy
  többájtos UTF-8 karakter (ékezet) közepén is. Ezért `TextDecoder`
  `{ stream: true }` módban, és a feldolgozatlan maradék pufferben marad a
  következő darabig.
- Az események üres sorral (`\n\n`) záródnak; a `data:` sorokból jön a JSON.
  A `\r\n` sorvéget is el kell fogadni.
- Az ismeretlen `type`-ú eseményt figyelmen kívül hagyjuk (a szerver később
  bővülhet). Az értelmezhetetlen JSON hiba.

Eseménytípusok és kezelésük:

| `type` | Mit tesz a kliens |
|---|---|
| `token` | a `text`-et hozzáfűzi a készülő válaszhoz |
| `sentence` | egyelőre semmit (a hangszintézisé lesz) |
| `done` | a válasz kész |
| `error` | a `message`-et megjeleníti a válasz alatt; a már megérkezett szöveg marad |

### Hibák

- **A stream előtti hibák:** a `response.ok`-ot a folyam olvasása előtt kell
  ellenőrizni. `401`: „Érvénytelen token” jellegű üzenet. Más státusz: a
  státuszkód megjelenik. Hálózati hiba (a szerver nem érhető el): érthető
  üzenet, a Tailscale/VPN irányába mutatva.
- **Megszakadt folyam:** ha a folyam `done` nélkül ér véget, a válasz
  „megszakadt”-ként jelenik meg, a szöveg marad.
- Hibás válasz után az üzenetküldés újra használható.

### Leállítás

„Leállítás” gomb generálás közben, `AbortController`-rel. A szerver ezt
már kezeli (a félbemaradt választ `partial`-ként naplózza). A kliensen a
megállított válasz szövege marad, jelölve, hogy leállítva.

### Felület

- Üzenetlista (a felhasználóé és Izzie-é megkülönböztetve), alatta beviteli
  mező és küldés gomb. `Enter` küld, `Shift+Enter` új sor.
- Generálás közben a küldés tiltva, a Leállítás látszik.
- Új üzenetnél a lista az aljára görget.
- A felület szövegei magyarul.
- Megjelenés: egyszerű, sötét téma, a sablon logói és stílusai eltűnnek.
  Kidolgozott design nem cél ebben a szeletben.
- Az ablak címe már `Izzie`.

### Állapot

A beszélgetés állapota a szerveren él. A kliens nem küld előzményt és nem
tárol semmit: újraindítás után az ablak üres, de Izzie emlékszik. Az
előzmény betöltése a szerverről későbbi szelet (új végpontot igényel).

### Tesztek

- Vitest, a bontó modulra: egy esemény egyben; több esemény egy darabban;
  esemény több darabra vágva (a sor közepén is); ékezetes karakter két
  darab között kettévágva; `\r\n` sorvég; ismeretlen típus; hibás JSON; a
  folyam vége maradék puffer mellett.
- A React-komponensre és a valódi hálózatra nincs automata teszt; azt a
  füstpróba fedi.
- `npm test` futtatja.

### Füstpróba

`npm run tauri dev`, majd:

1. Egy üzenet: a válasz szavanként jelenik meg.
2. Visszautalós kérdés: Izzie emlékszik (a szerveroldali memória működik a
   kliensen át is).
3. Egy hosszú válasz közben Leállítás: a szöveg megáll, jelölve.
4. Hibás token a `.env.local`-ban (Vite újraindítás után): érthető 401-es
   üzenet.
5. Leállított uvicorn mellett: érthető hálózati hibaüzenet.

**Eredmény (2026-09-16):** mind az öt lépés a várt módon működött.

### Megfigyelések a füstpróbából

- **Az üres buborék lefagyásnak látszik.** Az első token megérkezéséig a
  válaszbuborék üres, és semmi nem jelzi, hogy a kérés fut. Kell egy
  „Izzie gondolkodik…” jelzés az első tokenig. A következő szeletbe.
- **Késleltetés az uvicorn újraindítása után.** Egy újraindítás utáni első
  üzenetre a válasz nem indult el, és kézzel le lett állítva. Az ok nem
  tisztázott (induláskori háttérciklus, lassú első Gemini-válasz, vagy
  valódi hiba); a várakozási idő és az uvicorn-napló alapján kell
  eldönteni.
- A leállított, üres válasz nem került a naplóba, a két egymást követő
  user üzenetet a szerver egy fordulóba vonta — a 2. szelet normalizálása
  a várt módon működött.

## 2. szelet: várakozás és időkorlátok

### Miért

A füstpróbán egy kérés fél percnél tovább üres buborékkal állt, és kézzel
kellett leállítani. Az uvicorn-napló szerint a szerver elfogadta a kérést és
megnyitotta a folyamot (`200 OK`), utána semmi: se hiba, se figyelmeztetés.
A Gemini nem válaszolt, és **sem a szerver, sem a kliens nem ad fel soha.**
Ez a külső szolgáltatás oldalán bármikor újra előfordulhat, ezért nem a
hibát javítjuk, hanem felkészülünk rá.

Két rész, két repóban: előbb a szerver, utána a kliens.

**Állapot:** a szerver rész kész és élesben tesztelt (2026-09-16, a
rövidített korláttal a kliensben ékezetes hibaüzenet jelent meg). A kliens
rész következik.

### Szerver: időkorlát a Gemini-hívásokon

**Chat.** Két korlát, konstansként az `app/main.py`-ban:

- `FIRST_CHUNK_TIMEOUT_SECONDS = 30` — a hívás indításától az első
  darabig. Ide tartozik a stream megnyitása is.
- `CHUNK_IDLE_TIMEOUT_SECONDS = 30` — két darab között. Egy hosszú,
  folyamatosan érkező válasz így nem ütközik a korlátba, egy menet közben
  elakadt viszont igen.

Megvalósítás `asyncio.wait_for`-ral (vagy `asyncio.timeout`-tal) a stream
megnyitása és minden egyes következő darab kérése köré — nem az SDK saját
HTTP-timeoutjával, mert annak viselkedése streamelésnél nem egyértelmű, az
asyncio-s korlát viszont bármelyik fázisban elakadt hívást elkapja.

Lejáratkor:

- `error` esemény a kliensnek: „Izzie most nem kapott választ a
  modelltől. Próbáld újra.”
- A logba warning, a korlát típusával (első darab vagy menet közbeni).
- A már megérkezett szöveg a meglévő módon `partial`-ként naplózódik; ha
  nem jött semmi, a válasz nem kerül naplóba (mint eddig).
- A Gemini-stream lezárul, nem marad nyitott kapcsolat a háttérben.

**Extractor.** `EXTRACTOR_TIMEOUT_SECONDS = 120` a teljes hívásra. Lejárat
= sikertelen kísérlet, a meglévő úton (`extract_attempts` nő). Egy elakadt
kivonatolás így nem tartja fel a háttérciklust.

**Tesztek.** Hamis kliens, ami nem ad választ (illetve egy darab után
elakad): `error` esemény megy ki, az első esetben nincs assistant-sor, a
másodikban `partial` sor van a megérkezett szöveggel. Extractor: az elakadó
hívásból sikertelen kísérlet lesz. A tesztek rövid korláttal futnak
(konstans felülírása), ne várjanak valóban 30 másodpercet.

### Kliens: „gondolkodik” jelzés és biztonsági korlát

**Jelzés.** Amíg az első `token` meg nem érkezik, a válaszbuborékban egy
finoman animált „Izzie gondolkodik…” jelzés látszik. Az első tokennel
eltűnik. Leállításkor és hibánál is eltűnik.

**Biztonsági korlát.** Arra az esetre, ha a szerver maga akad el (és ezért
a saját `error` eseményét sem küldi el):

- `IDLE_TIMEOUT_MS = 45000` — ennyi ideig nem érkezik **semmilyen adat** a
  folyamon (bármilyen darab nullázza, nem csak a `token` esemény).
- Szándékosan hosszabb a szerver 30 másodpercénél: normál esetben a
  szerver `error` eseménye ér ide előbb, és az a pontosabb üzenet.
- Lejáratkor a kliens megszakítja a kérést, és új végkimenetellel zárja:
  `ending: "timeout"`. Megjelenítés: „Izzie nem válaszolt időben.”, a már
  megérkezett szöveg marad.
- A kérés elküldésétől számít, így a fejlécre várakozást is lefedi.
- A korlát a `chatClient`-ben él, nem a komponensben. A felhasználói
  leállítás és az időkorlát miatti megszakítás megkülönböztethető kell
  legyen (az egyik `aborted`, a másik `timeout`).

**Tesztek.** A bontó modul nem változik. Az időkorlátra nincs automata
teszt (a `chatClient` hálózati réteg); a füstpróba fedi.

### Füstpróba

1. Normál beszélgetés: a jelzés megjelenik, az első szóval eltűnik.
2. A szerver korlátjának próbája: átmenetileg
   `FIRST_CHUNK_TIMEOUT_SECONDS = 0.01`, egy üzenet → a szerver
   hibaüzenete jelenik meg. Utána visszaállítás.
3. A kliens korlátjának próbája: átmenetileg `IDLE_TIMEOUT_MS = 2000` és a
   szerveren egy mesterséges késleltetés — vagy egyszerűbben: a szerver
   folyamatát `kill -STOP`-pal megállítva, majd `kill -CONT`-tal
   folytatva. → „Izzie nem válaszolt időben.” Utána visszaállítás.

A szerver felfüggesztése a gyakorlatban: a tmux ablakban `Ctrl+Z`, folytatás
`fg`. Ez a `--reload` alatt futó munkafolyamatot is megállítja, PID-keresés
nélkül.

**Eredmény (2026-09-17):** mindhárom lépés a várt módon működött. A
jelzés megjelent és eltűnt, a szerver korlátja a saját hibaüzenetét adta,
a kliens korlátja „Izzie nem válaszolt időben.”-t mutatott (nem
„Leállítva”-t). A kliens rész kész.

### Megfigyelések a füstpróbából

- **Gemini 503 (túlterhelt modell).** A próba közben a Gemini átmenetileg
  `503 UNAVAILABLE`-t adott. A kliensben ez az általános „Hiba történt a
  válasz generálása közben.” üzenetként jelent meg. Pontosabb üzenet kell,
  pl. „A modell most túlterhelt, próbáld újra később.” A következő szerver
  oldali körbe.
- **Megválaszolva (2026-09-17), lásd `docs/llm.md`:** az SDK alapból nem
  próbálkozik újra, és a stream megnyitása a korláton belül van. Az extractor
  időkorlátja ott átmeneti hibává válik (nem számít kísérletnek).
- **Nyitott kérdés: az SDK újrapróbálkozása és az időkorlát.** A
  `google-genai` 503-ra magától újrapróbálkozik (`tenacity`, növekvő
  várakozással). Nem ellenőriztük, hogy ez a várakozás teljes egészében a
  szerver `FIRST_CHUNK_TIMEOUT_SECONDS` korlátja alá esik-e. A 503-as
  hibaüzenet javításakor ezt is meg kell nézni (és eldönteni, kell-e az SDK
  saját újrapróbálkozása egyáltalán, ha a korlát úgyis lezárja a kérést).

## Költözés Ubuntura (2026-10-24)

A laptop Windowsról Ubuntura váltott. Ami ebből következik:

- **Útvonalak:** lásd „Felállás”. A specek sima fájlok ebben a repóban,
  Obsidian nélkül.
- **Rust-gyorsítótár áthelyezés után:** a `src-tauri/target` abszolút
  útvonalakat tárol. Ha a kliens mappája elköltözik, az első fordítás
  „failed to read plugin permissions” hibával bukik. Megoldás:
  `cargo clean` a `src-tauri`-ban.
- **GitHub-hozzáférés a laptopon:** `gh auth login` (HTTPS, böngészős
  bejelentkezés). A szerver továbbra is SSH-t használ.
- **VPN:** a ProtonVPN helyett NordVPN van, nem mindig bekapcsolva. Ha
  bekapcsolt NordVPN mellett a kliens nem éri el a szervert, valószínűleg
  ugyanaz a jelenség, mint a ProtonVPN-nél; a megoldás a `100.64.0.0/10`
  tartomány kivétele (NordVPN Linuxon: `nordvpn allowlist add subnet
  100.64.0.0/10`). Még nem próbáltuk ki.
- **CORS (kész):** az alapértelmezett eredetek közé
  (`_DEFAULT_CORS_ORIGINS` az `app/main.py`-ban, és az `.env.example`
  megjegyzése) bekerült a `tauri://localhost`. A `http://tauri.localhost`
  maradt (Android). `tauri dev` alatt nem számít, az első `tauri build`
  előtt kell.

## 3. szelet: előzmény betöltése

### Miért

A kliens minden indításkor üres ablakkal nyit, pedig Izzie emlékszik: a
beszélgetés a szerveren él. Indításkor a kliens töltse be a legutóbbi
üzeneteket, hogy ott folytatódjon, ahol abbamaradt.

Két rész, két repóban: előbb a szerver, utána a kliens.

### Szerver: `GET /history`

**Végpont.** `GET /history?limit=50`, a `protected_router`-en (tokennel).
JSON-t ad, nem SSE-t: egyszeri, kis válasz, nincs mit streamelni.

```json
{
  "messages": [
    {
      "id": 812,
      "session_id": 37,
      "role": "user",
      "content": "Szia Izzie!",
      "status": "complete",
      "created_at": "2026-10-24T17:30:12+00:00"
    }
  ]
}
```

- **A legutóbbi `limit` üzenet, sessionöktől függetlenül**, időrendben (a
  legrégebbi elöl). Nem csak az aktuális session: a felhasználó a korábbi
  beszélgetéseket is látni akarja, akkor is, ha Izzie azokra már csak a
  tényeken és az összefoglalón át emlékszik.
- `limit`: alapértelmezés 50, megengedett 1–200. Ezen kívül `422`
  (FastAPI `Query(ge=1, le=200)`).
- **A redaktált üzenetek kimaradnak**, a `partial`-ok bent maradnak a
  státuszukkal — ugyanaz a szabály, mint a `get_history()`-ban.
- Sorrend: `created_at`, azonos időnél `id` szerint — a `get_history()`
  mintájára.
- A `created_at` úgy megy ki, ahogy a DB-ben van (ISO 8601, UTC). A
  megjelenítés a kliens dolga.

**Mellékhatás nincs.** A végpont csak olvas: nem nyit sessiont, nem zár le
lejártat (`resolve_session` és `close_expired_sessions` nem hívódik), és
nem ír semmit. Egy kliens-indítás így nem befolyásolja, mikor kezdődik új
beszélgetés.

**Hol él.** A lekérdezés a `memory.py`-ban (pl.
`get_recent_messages(conn, limit)`, a többi lekérdező mintájára
kapcsolatot kap), mellé egy async belépési pont, ami `to_thread`-ben fut.
A `main.py`-ban csak a végpont és a bekötés.

**Hibák.** DB-hiba esetén `500`, a részletek csak a logba kerülnek, a
válaszba nem.

**Tesztek.**

- lekérdezés: több session üzenetei időrendben; a `limit` a legutolsó N-et
  tartja meg; redaktált kimarad; `partial` bent marad a státuszával; üres
  DB → üres lista
- végpont: token nélkül `401`; alapértelmezett `limit`; `limit=0` és
  `limit=201` → `422`
- **a hívás nem hoz létre sessiont** (a `sessions` sorainak száma nem
  változik) — ez a szelet egyik lényege, ezért külön teszt

### Kliens: betöltés indításkor

**Hálózat.** A `chatClient.ts` mellé egy `fetchHistory()` (vagy hasonló),
ugyanazzal a címmel és tokennel. A hibák osztályozása ugyanaz, mint a
chatnél (`401`, más státusz, hálózati hiba, a `ChatRequestError`
újrahasznosítható). **Időkorlát: 15 másodperc** a teljes kérésre — a 2.
szelet tanulsága, hogy semmi ne várjon örökké.

**Átalakítás: tiszta függvény, külön modulban** (pl. `src/history.ts`). A
szerver üzeneteiből a felület elemeit állítja elő, Reacttől és hálózattól
függetlenül:

- `role`: `user` → felhasználó, `assistant` → Izzie.
- `complete` → kész üzenet. `partial` → kész, de **„Félbemaradt”**
  jelöléssel. (A szerver nem tárolja, hogy leállítás, megszakadás vagy
  időkorlát volt-e az ok, ezért egy közös felirat.)
- Az azonosító a szerverét követi (pl. `srv-812`), hogy ne ütközzön a
  kliens saját `crypto.randomUUID()` azonosítóival.
- **Session-elválasztó:** minden session első betöltött üzenete elé egy
  elválasztó kerül a dátummal és idővel, helyi időben, magyarul (pl.
  „2026. október 24. 19:30”, `Intl.DateTimeFormat("hu-HU", …)`). A lista
  elején is van egy. Az elválasztó azt is jelzi, hol kezdődik egy új
  beszélgetés, amire Izzie már nem szó szerint emlékszik.

**Állapot és felület.**

- Betöltés közben a lista helyén halvány „Előzmény betöltése…” felirat,
  és **a küldés tiltva**. Ha közben elküldhető lenne egy üzenet, az
  előzmény fölé kerülne, és a sorrend összekeveredne.
- Siker: az előzmény bekerül a listába, az aljára görget (a meglévő
  görgetés ezt lefedi).
- Hiba: a lista tetején halvány hibasor, pl. „Az előzményt nem sikerült
  betölteni: <ok>.” **A küldés ilyenkor is működik**: az előzmény hiánya
  nem akadálya a beszélgetésnek. Újrapróbálás gomb nincs, az újraindítás
  megoldja.
- Az új üzenetek az előzmény alá kerülnek, elválasztó nélkül.

**Buktató: `StrictMode`.** Fejlesztéskor a React az effekteket kétszer
futtatja. Az effekt ezért `AbortController`-rel takarítson maga után,
különben két kérés indul, és az előzmény kétszer kerülhet be a listába.

**Ismert korlát, szándékosan.** Ha egy válasz nem került a naplóba (üres
leállítás vagy hiba még az első szó előtt), az előzményben a felhasználó
üzenete válasz nélkül áll. Ez a napló valós állapota, nem töltjük ki.

**Tesztek.** Vitest az átalakító függvényre: üres bemenet; egy session
(egy elválasztó, utána az üzenetek); két session (két elválasztó, jó
helyen); a szerepkörök leképezése; `partial` jelölése; a sorrend
megmarad; az azonosítók. A dátum formázása időzónától függ: a teszt vagy
rögzített időzónával fut, vagy a formázó külön függvény, és a
szerkezeti tesztek nem a formázott szövegre építenek.

### Füstpróba

1. A kliens indítása: megjelenik az előzmény, elválasztókkal, alul a
   legutóbbi üzenettel.
2. Egy új üzenet: az előzmény alá kerül, Izzie válaszol.
3. A kliens újraindítása: az előző lépés üzenete is ott van.
4. Leállított uvicorn mellett indítva: a hibasor megjelenik, a felület
   használható (a küldés a szerver nélkül persze hibát ad).
5. A sessionök száma a kliens indítása előtt és után nem változik. A
   szerveren, a projekt mappájában:
   `venv/bin/python -c "import sqlite3; print(sqlite3.connect('data/izzie.db').execute('SELECT COUNT(*) FROM sessions').fetchone()[0])"`
