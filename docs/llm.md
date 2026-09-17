# LLM-hívások

A Gemini-hívások közös szabályai (chat és extractor). Később ide kerül a
provider-réteg (`ask_llm()`) is; most csak a hibakezelés.

Állapot: az 1. szelet spece kész, a megvalósítás következik.

## 1. szelet: átmeneti hibák

### Miért

A kliens füstpróbáján (2026-09-17) a Gemini `503 UNAVAILABLE`-t adott
(„high demand”). Két gond derült ki:

- **A chatben** a felhasználó az általános „Hiba történt a válasz
  generálása közben.” üzenetet látta, és egy teljes traceback került a
  logba, holott ez egy várható, külső, átmeneti hiba.
- **Az extractorban** ugyanez sikertelen kísérletnek számít. Három bukott
  kísérlet után a session véglegesen kimarad. A háttérciklus ötpercenként
  fut, vagyis **egy negyedórás Gemini-kimaradás a közben lezárult
  sessionöket csendben, végleg kizárja a tanulásból.**

### Amit a kódból tudunk (google-genai 2.22.0)

- **Az SDK alapból nem próbálkozik újra.** `retry_options` nélkül a
  `tenacity` egyetlen kísérletre van állítva; a logban látszó `tenacity`
  csak a burkoló.
- **Az újrapróbálkozás a stream megnyitására vonatkozik.** Az a `main.py`-ban
  már most a `FIRST_CHUNK_TIMEOUT_SECONDS` korláton belül van, tehát egy
  beállított újrapróbálkozás sem tudja kijátszani a korlátot.
- A `200 OK` utáni, menet közbeni hibát az SDK nem próbálja újra. Ez így
  helyes: addigra már ment ki szöveg a kliensnek.

### Rövid újrapróbálkozás

A `genai.Client` létrehozásakor:

```python
types.HttpOptions(
    retry_options=types.HttpRetryOptions(
        attempts=3,
        http_status_codes=[500, 502, 503, 504],
    )
)
```

Mivel a chat és az extractor ugyanazt a klienst használja, mindkettőre
érvényes.

- **Miért 3 kísérlet:** a várakozás kb. 1 és 2 másodperc (plusz legfeljebb
  1-1 mp véletlen), bőven a 30 másodperces korlát alatt. A „high demand”
  503 gyakran pár másodperc alatt elmúlik. Az SDK alapértéke (5 kísérlet,
  1+2+4+8 mp) a kérések idejével együtt könnyen a korlátba futna, és akkor
  a pontatlanabb „nem kapott választ” üzenet jönne.
- **Miért nem 429:** az a használati keret (percenkénti vagy napi) kimerülése.
  Pár másodperc várakozás nem segít rajta, csak újabb kérést küld.
- **Veszélytelen:** az újrapróbálkozás az első darab előtt történik, a
  kliens addig semmit nem kapott, és a naplóba sem került semmi.

### Hibák osztályozása: közös modul

Új modul: `app/llm.py`. Ide költözik a kliens létrehozása, és itt él két
tiszta függvény, amit a chat és az extractor is használ:

- `is_transient_error(exc) -> bool` — igaz, ha:
  - `APIError`, `429` vagy `5xx` kóddal,
  - `TimeoutError` (az asyncio-s korlátok),
  - hálózati hiba (a kapcsolat nem jött létre vagy megszakadt). Hogy ez
    pontosan melyik kivételosztály, az az SDK által használt HTTP-rétegtől
    függ (`httpx` vagy `aiohttp`); ezt a megvalósításkor kell ellenőrizni.
- `describe_error(exc) -> str` — a felhasználónak szóló üzenet:

| Hiba | Üzenet |
|---|---|
| `5xx` | „A modell most túlterhelt vagy nem elérhető. Próbáld újra egy kicsit később.” |
| `429` | „Elérted a modell használati keretét. Próbáld újra később.” |
| minden más | „Hiba történt a válasz generálása közben.” (a mostani) |

Az üzenetszövegek a `_LENGTH_HINT` kategóriájába esnek: kódban maradnak,
nem persona-tartalom. Az időkorlát saját üzenete (`_handle_stream_timeout`)
nem változik.

Ez a modul a későbbi `ask_llm()` réteg csírája, de most **csak ennyi**:
provider-absztrakciót nem építünk előre.

### Chat

A `generate()` `APIError` ága a `describe_error()` üzenetét küldi.
Naplózás:

- átmeneti hiba: `warning`, a kóddal és a státusszal, **traceback nélkül**
  (várt, külső hiba, a traceback csak elfedi a logban a valódi gondokat);
- minden más: `logger.exception`, mint most.

A naplózás (`partial`, üres válasz nem kerül be) nem változik.

### Extractor

**Átmeneti hiba nem számít kísérletnek.** Ilyenkor:

- `extract_attempts` **nem** nő, `extracted_at` üres marad;
- `warning` a logba;
- **a kör megszakad**, a további sessionök ebben a körben nem kerülnek
  sorra. Ha a modell túlterhelt, a következő session is elbukna, és
  feleslegesen terhelnénk. Öt perc múlva a következő kör újrakezdi.

Minden más hiba (értelmezhetetlen JSON, validálás, `400`/`403`, írási hiba)
a meglévő módon kísérletnek számít, három után a session kimarad.

Tudatos kockázat: egy tartósan „átmeneti” hiba (pl. napokig kimerült keret)
végtelen próbálkozást jelent. Ez körönként egyetlen hívás ötpercenként, ami
nem égeti a kvótát, és a warning a logban látszik.

### Tesztek

- `is_transient_error` / `describe_error`: `503`, `500`, `429`, `400`,
  `403`, `TimeoutError`, hálózati hiba, egyéb kivétel.
- Az újrapróbálkozási beállítás konstansként él; a teszt azt ellenőrzi
  (a kísérletszám és a kódlista), nem az SDK viselkedését.
- Chat: hamis kliens, ami a stream megnyitásakor `503`-at dob → `error`
  esemény a túlterhelt-üzenettel, nincs assistant-sor. `400` → az
  általános üzenet.
- Extractor: `503` és időkorlát → `extract_attempts` nem nő, és a második
  session nem kerül hívásra ugyanabban a körben. Hibás JSON és `400` →
  `extract_attempts` nő (a meglévő viselkedés).

### Füstpróba

A `503` élesben nem idézhető elő, azt a tesztek fedik; a következő valódi
túlterhelésnél a logban és a kliensben is látszani fog.

1. Normál beszélgetés a kliensből: működik (az újrapróbálkozási beállítás
   nem rontott el semmit).
2. Átmenetileg hibás modellnév (`MODEL_NAME`) → a kliensben az általános
   üzenet, a logban traceback (nem átmeneti hiba). Utána visszaállítás.
