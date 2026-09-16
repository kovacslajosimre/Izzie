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
