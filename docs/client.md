# Kliens

Állapot: a Tauri váz kész (`kovacslajosimre/Izzie-client`), a szerver
előkészítése (token, CORS, Tailscale) kész és élesben tesztelt. A kliens
szeletei ezután kerülnek ide.

## Felállás

- **Kliens:** Tauri 2 + React + TypeScript, külön repóban
  (`kovacslajosimre/Izzie-client`), a laptopon a vaulton kívül
  (`C:\Users\LamaLT\projects\Izzie-client`). Azért nem a vaultban, mert a
  `node_modules` és a Rust `target` mappája több gigabájt és több tízezer
  fájl: a git kiszűri, az Obsidian nem.
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
- `http://tauri.localhost` — a lefordított alkalmazás Windowson

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

### Ismert buktató: ProtonVPN a laptopon

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
  első `tauri build` előtt a token a Windows jelszótárába költözik (külön
  szelet).

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
