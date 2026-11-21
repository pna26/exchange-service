# exchange-service

Microsserviço de **câmbio** da plataforma [pna26](https://github.com/pna26/platform) (Python, FastAPI).
Responsável: Gabriel ([@gabrielalmeidaconde](https://github.com/gabrielalmeidaconde)).

```
GET /exchange/{from}/{to}        header id-account (injetado pelo gateway)
-> { "sell": 5.3942, "buy": 5.3912, "date": "2026-11-05 10:00:00", "id-account": "42" }
```

| Status | Quando |
|---|---|
| 200 | cotação (do cache ou da API externa) |
| 400 | moeda que não tem 3 letras |
| 401 | sem header `id-account` (a requisição não passou pelo gateway) |
| 404 | par de moedas que a API externa não conhece |
| 503 | AwesomeAPI e PTAX fora do ar e sem cotação recente em cache |

A cotação vem da [AwesomeAPI](https://docs.awesomeapi.com.br/api-de-moedas) (`ask` → `sell`, `bid` → `buy`)
e fica em cache na memória por `CACHE_TTL` segundos. Detalhes na
[documentação](https://pna26.github.io/platform/servicos/exchange/).

| Variável | Padrão | |
|---|---|---|
| `EXCHANGE_API_URL` | `https://economia.awesomeapi.com.br/json/last` | API externa |
| `EXCHANGE_API_TOKEN` | — | token da AwesomeAPI (opcional, aumenta o limite) |
| `CACHE_TTL` | `30` | segundos que uma cotação vale (0 desliga o cache) |
| `CACHE_STALE_TTL` | `600` | por quanto tempo uma cotação velha ainda serve se a API cair |
| `UPSTREAM_TIMEOUT` | `3` | timeout da chamada externa (s) |
| `EXCHANGE_FALLBACK` | `ptax` | fallback quando a AwesomeAPI falha (`none` desliga) |
| `PTAX_BASE_URL` | `https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata` | API PTAX do Banco Central |

### Fallback PTAX (Banco Central)

No EKS a AwesomeAPI responde 429 para o IP do NAT da AWS (o plano gratuito limita IPs de nuvem).
Por isso, quando ela falha (timeout, 5xx, 429 ou resposta inválida), o serviço busca a cotação
**PTAX** na [API Olinda do BACEN](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/aplicacao#!/recursos),
pública e sem cadastro. Decisões:

- pega o último boletim de **Fechamento** dos últimos 7 dias (fim de semana e feriado não têm
  cotação); `cotacaoVenda` → `sell`, `cotacaoCompra` → `buy`. A PTAX é a média do dia anterior,
  não o preço em tempo real: serve como plano B, não como fonte principal;
- a PTAX só cota 10 moedas contra o real (AUD, CAD, CHF, DKK, EUR, GBP, JPY, NOK, SEK, USD):
  `X→BRL` direto, `BRL→X` invertido e `X→Y` cruzado via BRL. Par fora disso continua 503;
- 404 da AwesomeAPI **não** aciona o fallback (ela conhece mais moedas que o BACEN);
- o contrato da rota não muda; a cotação do fallback também vai para o cache.

Métricas: `exchange_upstream_request_seconds{outcome}` continua sendo só a AwesomeAPI;
`exchange_ptax_request_seconds{outcome}` mede o BACEN e `exchange_fallback_total{provider,outcome}`
conta quantas vezes o fallback foi usado (`ok`, `error`, `unsupported`).

Outras rotas: `/health` e `/exchange/health-check` (probes), `/metrics` (Prometheus, só rede interna),
`/exchange/docs` (Swagger).

## Rodar

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
uvicorn app.main:app --port 8080 --reload
curl -H 'id-account: 1' localhost:8080/exchange/USD/BRL
```

Na plataforma inteira: `docker compose --profile exchange up -d --build` no repositório `platform`.

## CI/CD

`.github/workflows/ci.yaml` chama o pipeline reutilizável do `platform` (pytest, Trivy, imagem no
GHCR/ECR, deploy no EKS com `k8s/k8s.yaml`).
