# Hermes Router — darmowy AI router na VPS

LiteLLM proxy za nginx (HTTPS) udostępniający jeden endpoint OpenAI-compatible
dla Hermesa, z fallbackiem między trzema darmowymi providerami.

## Architektura

```
Hermes ──HTTPS──▶ nginx :443 (<YOUR_HOST>.sslip.io)
                    │  Bearer ROUTER_API_KEY + rate limit 10 r/m
                    ▼
        LiteLLM (Docker, 127.0.0.1:4000)
          aliasy: free-fast / free-general / free-coding / free-fallback
                    │
     ┌──────────────┼───────────────────┐
 Nous Portal   OpenRouter        OpenCode Zen
```

## Providery i modele

Łańcuchy aliasów nie są tu ręcznie utrzymywane — patrz
**„Auto-audit łańcuchów"** niżej oraz aktualny stan na
`http://<YOUR_HOST>:8080/comparisons-router-changelog.html`.

Aktualne zasady (sierpień 2026):
- **OpenCode Zen wymaga teraz prawdziwego klucza API** (`OPENCODE_FREE_API_KEY`).
  Wcześniej był keyless (`dummy-keyless`) — to już nie działa (401 AuthError).
- **Nous Portal** wymaga sufiksu `:free` w ID modeli; bez niego idzie na billing.
- **OpenRouter Free** ma ~20 req/min wspólnego limitu — jest fallbackiem, nie primary.
- Modele stealth mogą zniknąć bez zapowiedzi — dlatego każdy alias ma 3-5 fallbacków.
- Rate limit: nginx 10 req/min per IP (zone w `/etc/nginx/conf.d/router-limit.conf`).
- **Wiki model-wiki** jest serwowane pod `https://<YOUR_HOST>.sslip.io/wiki/` (proxy
  do `/var/www/model-wiki/`) — m.in. strona changelogu routera łańcuchów.

## Auto-audit łańcuchów (codziennie 06:00 UTC)

`/opt/hermes-router/scripts/router_audit.py` (via cron `0600-utc-daily-router-audit.sh`):
1. **Fetch** — free modele (price==0) z OpenRouter, Nous Portal i OpenCode Zen
   (ceny są stringami — konwersja na float; image-only modele wykluczone).
2. **Ranking** — cross-referencja źródeł + context length + priorytet providera +
   kategoria (coding / fast / general). Czarne listy: GLM-5.2 (429), Inkling (403),
   Step-3.7-flash (płatny), Hy3 (nigdzie nie jest free), Lyria (obraz).
3. **Diff** vs obecny `config.yaml`.
4. **Apply** — backup do `configs/<timestamp>/`, zapis, restart, health-check,
   smoke-test **wszystkich 4 aliasów** przez router, rollback przy porażce.
5. **Changelog** — `data/changelog.json` (ostatnie 30 wpisów) → strona wiki.

Ręcznie: `python3 /opt/hermes-router/scripts/router_audit.py --dry-run` (podgląd)
lub `--apply` (wdróż). Test pojedynczego modelu: `scripts/smoke_test.py <id>`.

## Konfiguracja

Sekrety w `.env` (chmod 600, nigdy w git/logach). Wzorzec: `.env.example`.
Definicje modeli/aliasów/fallbacków: `config.yaml` (generowany przez audyt).

## Operacje

```bash
cd /opt/hermes-router
docker compose up -d              # start / restart po zmianach
docker compose restart litellm    # sam restart
docker compose logs -f litellm    # logi (bez treści promptów; sekrety nie są logowane)
docker compose pull && docker compose up -d   # aktualizacja obrazu
curl -s https://<YOUR_HOST>.sslip.io/health/liveliness   # health
```

## Sprawdzenie użycia providera/modelu

LiteLLM loguje per request: wybrany deployment (provider/model), latencję,
fallbacki i błędy 429/5xx. `docker compose logs litellm | grep free-general`.

## Dodanie providera / zmiana modelu

1. Dodaj wpis w `config.yaml` (`model_list`) z `api_base` + `api_key` env,
   albo w `scripts/router_audit.py` (np. do czarnej listy / mapy źródło→klucz).
2. Dopisz env do `.env` (+ `.env.example` bez wartości).
3. `docker compose up -d`.
Zasada: **tylko modele price==0** — nowy model musi być zweryfikowany jako darmowy.

## Konfiguracja Hermesa

```bash
# named provider 'router' (NIE 'custom') — Desktop picker wtedy widzi 4 aliasy
hermes config set model.default free-general
hermes config set model.provider router
# providers.router w config.yaml Hermesa: api+key_env+discover_models:false
#   + models: {free-general, free-fast, free-coding, free-fallback} z context_length
```

Uwaga: wybór modelu w Desktop UI nadpisuje sekcję `model:` w config.yaml Hermesa
(pomija router). Przywrócenie: dwie komendy `hermes config set` powyżej.