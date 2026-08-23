# Hermes Router — darmowy AI router na VPS

LiteLLM proxy za nginx (HTTPS) udostępniający jeden endpoint OpenAI-compatible
dla Hermesa, z fallbackiem między trzema darmowymi providerami.

## Architektura

```
Hermes ──HTTPS──▶ nginx :443 (135-125-233-237.sslip.io)
                    │  Bearer ROUTER_API_KEY + rate limit 10 r/m
                    ▼
        LiteLLM (Docker, 127.0.0.1:4000)
          aliasy: free-fast / free-general / free-coding / free-fallback
                    │
     ┌──────────────┼───────────────────┐
 Nous Portal   OpenRouter        OpenCode Free (keyless)
```

## Providery i modele (whitelist, wszystkie price==0)

| Alias | Kolejność fallback |
|---|---|
| free-fast | nemotron-3.5-lightning-free (OpenCode) → step-3.7-flash (Nous) → nemotron-3.5-lightning:free (OR) |
| free-general | stealth/ox-alpha (Nous) → z-ai/glm-5.2:free (OR) → nemotron-3-ultra:free (OR) → meituan/longcat-2.0:free (Nous) |
| free-coding | laguna-s-2.1-free (OpenCode) → mimo-v2.5-free (OpenCode) → poolside/laguna-s-2.1:free (OR) |
| free-fallback | hy3-free (OpenCode) → big-pickle (OpenCode) → tencent/hy3:free (Nous) |

Uwaga: OpenCode Free **odrzuca prawdziwe klucze** — LiteLLM wysyła dummy
`OPENCODE_FREE_API_KEY=dummy-keyless`. Nous Portal wymaga sufiksu `:free`
w ID modeli. Modele stealth (ox-alpha, big-pickle,
x-preview-f) mogą zniknąć bez zapowiedzi — dlatego każdy alias ma fallbacki.
Rate limit: nginx 10 req/min per IP (zone w `/etc/nginx/conf.d/router-limit.conf`).

## Konfiguracja

Sekrety w `.env` (chmod 600, nigdy w git/logach). Wzorzec: `.env.example`.
Definicje modeli/aliasów/fallbacków: `config.yaml`.

## Operacje

```bash
cd /opt/hermes-router
docker compose up -d              # start / restart po zmianach
docker compose restart litellm    # sam restart
docker compose logs -f litellm    # logi (bez treści promptów; sekrety nie są logowane)
docker compose pull && docker compose up -d   # aktualizacja obrazu
curl -s https://135-125-233-237.sslip.io/health/liveliness   # health
```

## Sprawdzenie użycia providera/modelu

LiteLLM loguje per request: wybrany deployment (provider/model), latencję,
fallbacki i błędy 429/5xx. `docker compose logs litellm | grep free-general`.

## Dodanie providera / zmiana modelu

1. Dodaj wpis w `config.yaml` (`model_list`) z `api_base` + `api_key` env.
2. Dopisz env do `.env` (+ `.env.example` bez wartości).
3. `docker compose up -d`.
Zasada: **tylko modele price==0** — nowy model musi być zweryfikowany jako darmowy.

## Konfiguracja Hermesa

```yaml
model:
  provider: custom
  base_url: 'https://135-125-233-237.sslip.io/v1'
  default: free-general        # | free-fast | free-coding | free-fallback
  api_key_env: ROUTER_API_KEY  # klucz z .env Hermesa; NIE klucze providerów!
```
(ustawione przez `hermes config set model.provider/base_url/default/api_key_env`)

## Płatne modele później

Wystarczy dopisać wpisy w `config.yaml` + klucze do `.env`. Hermes dalej
widzi te same aliasy — zero zmian po stronie klienta.
