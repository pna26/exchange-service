# exchange-service

Microsserviço de **câmbio** da plataforma [pna26](https://github.com/pna26/platform) (Python, FastAPI).
Responsável: Gabriel ([@gabrielalmeidaconde](https://github.com/gabrielalmeidaconde)).

```
GET /exchange/{from}/{to}        header id-account (injetado pelo gateway)
-> { "sell": 0.910763, "buy": 0.910487, "date": "2026-11-19 13:04:18", "id-account": "42" }
   header X-Cache: miss | hit | stale

GET /exchange/currencies
-> [ { "symbol": "USD", "name": "Dólar dos Estados Unidos" }, ... ]
```

`GET /exchanges/{from}/{to}` é um alias (o roteiro do curso usa esse caminho; o gateway já reescreve).
Detalhes na [documentação](https://pna26.github.io/platform/servicos/exchange/).

| Status | Quando |
|---|---|
| 200 | cotação (do cache ou da PTAX) |
| 400 | moeda que não tem 3 letras |
| 401 | sem header `id-account` (a requisição não passou pelo gateway) |
| 404 | moeda desconhecida (fora da PTAX, ou o BACEN devolveu lista vazia) |
| 503 | PTAX fora do ar e sem cotação em cache; também em `/exchange/currencies` |

O roteiro do curso devolve 502 quando a PTAX falha; mantivemos **503**, que é o contrato da
plataforma e o que o smoke test do `platform` espera.

### Provedor: PTAX do Banco Central

As APIs de câmbio gratuitas (AwesomeAPI etc.) não respondem para IPs da AWS (no EKS a AwesomeAPI
devolve 429 para o IP do NAT). Por isso a cotação vem da **PTAX** pela
[API Olinda do BACEN](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/aplicacao#!/recursos),
pública e sem cadastro, como no [roteiro do curso](https://insper.github.io/platform/pt/2026.2/exercises/exchange/bacen/):

- `CotacaoMoedaPeriodo` numa janela de 7 dias; usa o **último boletim** da lista (`cotacaoVenda`,
  `cotacaoCompra`, `dataHoraCotacao`);
- a PTAX só cota moeda → BRL. BRL vale 1 e qualquer par sai pela taxa cruzada via BRL:
  `sell = venda(from) / compra(to)`, `buy = compra(from) / venda(to)` (6 casas). Só 10 moedas:
  AUD, CAD, CHF, DKK, EUR, GBP, JPY, NOK, SEK, USD (e BRL);
- **limites**: o BACEN só publica em dias úteis, entre ~10h e ~13h. Fora disso a resposta é o
  último boletim publicado (pode ser de ontem ou de sexta). Não é cotação em tempo real;
- **não faça teste de carga contra a PTAX** e mantenha o cache ligado: com `CACHE_TTL=30`, o k6
  bate no cache e o BACEN recebe no máximo ~1 chamada por par a cada 30 s por pod.

Header `X-Cache`: `miss` (chamou a PTAX), `hit` (veio da memória) ou `stale` (a PTAX falhou e
foi servida a última cotação, até `CACHE_STALE_TTL`). A mesma informação vai para a métrica
`exchange_quote_cache_total{result}`.

| Variável | Padrão | |
|---|---|---|
| `EXCHANGE_PROVIDER` | `ptax` | `ptax` ou `awesomeapi` (modo antigo) |
| `PTAX_URL` | `https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata` | API PTAX do BACEN |
| `CACHE_TTL` | `30` | segundos que uma cotação vale (0 desliga o cache) |
| `CACHE_STALE_TTL` | `600` | por quanto tempo uma cotação velha ainda serve se a API cair |
| `UPSTREAM_TIMEOUT` | `3` | timeout das chamadas externas (s) |
| `EXCHANGE_API_URL` | `https://economia.awesomeapi.com.br/json/last` | só no modo `awesomeapi` |
| `EXCHANGE_API_TOKEN` | — | só no modo `awesomeapi` (token da AwesomeAPI, opcional) |
| `EXCHANGE_FALLBACK` | `ptax` | só no modo `awesomeapi`: usa a PTAX se a AwesomeAPI falhar (`none` desliga) |

No modo `awesomeapi`, um 404 da AwesomeAPI não aciona o fallback (ela conhece mais moedas que o BACEN).

Métricas: `exchange_upstream_request_seconds{outcome}` mede o provedor **principal** (a PTAX no
modo padrão; é o painel "API externa" do Grafana). No modo `awesomeapi`,
`exchange_ptax_request_seconds{outcome}` mede a PTAX como fallback e
`exchange_fallback_total{provider,outcome}` conta quantas vezes ela foi usada.

Outras rotas: `/health` e `/exchange/health-check` (probes), `/metrics` (Prometheus, só rede interna),
`/exchange/docs` (Swagger).

## Rodar

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
uvicorn app.main:app --port 8080 --reload
curl -i -H 'id-account: 1' localhost:8080/exchange/USD/EUR
```

O roteiro do curso usa `uv`/`pyproject.toml`; aqui ficamos com `pip` + `requirements.txt`
porque o pipeline reutilizável do `platform` (pytest, Trivy, imagem) instala assim.

Na plataforma inteira: `docker compose --profile exchange up -d --build` no repositório `platform`.

## CI/CD

`.github/workflows/ci.yaml` chama o pipeline reutilizável do `platform` (pytest, Trivy, imagem no
GHCR/ECR, deploy no EKS com `k8s/k8s.yaml`).
