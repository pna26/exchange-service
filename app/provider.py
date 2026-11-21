"""Clientes das APIs de câmbio: PTAX do Banco Central (principal, EXCHANGE_PROVIDER=ptax)
e AwesomeAPI (EXCHANGE_PROVIDER=awesomeapi, com a PTAX como fallback opcional).

AwesomeAPI:

    GET https://economia.awesomeapi.com.br/json/last/USD-BRL
    -> {"USDBRL": {"bid": "5.39", "ask": "5.40", "create_date": "2026-11-05 10:00:00", ...}}

"bid" é o preço pelo qual o mercado compra a moeda (buy) e "ask" o preço pelo
qual vende (sell); por isso sell >= buy.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from prometheus_client import Counter, Histogram

_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5)

# provedor PRINCIPAL, seja ele qual for (o painel "API externa" do Grafana usa esta)
UPSTREAM_SECONDS = Histogram(
    "exchange_upstream_request_seconds",
    "Tempo das chamadas à API de câmbio externa (provedor principal)",
    ["outcome"],  # ok | not_found | error
    buckets=_BUCKETS,
)
# PTAX quando ela é só o fallback da AwesomeAPI
PTAX_FALLBACK_SECONDS = Histogram(
    "exchange_ptax_request_seconds",
    "Tempo das chamadas à PTAX quando ela é o fallback da AwesomeAPI",
    ["outcome"],
    buckets=_BUCKETS,
)


@dataclass(frozen=True)
class Quote:
    sell: float
    buy: float
    date: str


class PairNotFound(Exception):
    """A API externa não conhece o par de moedas."""


class UpstreamError(Exception):
    """A API externa falhou (timeout, 5xx, limite de requisições, resposta inválida)."""


class AwesomeApiProvider:
    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str | None = None,
                 metric: Histogram = UPSTREAM_SECONDS):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._metric = metric

    async def fetch(self, src: str, dst: str) -> Quote:
        params = {"token": self._token} if self._token else None
        start = time.perf_counter()
        outcome = "error"
        try:
            try:
                resp = await self._client.get(f"{self._base_url}/{src}-{dst}", params=params)
            except httpx.HTTPError as e:
                raise UpstreamError(str(e)) from e

            if resp.status_code == 404:
                outcome = "not_found"
                raise PairNotFound(f"{src}-{dst}")
            if resp.status_code != 200:
                raise UpstreamError(f"status {resp.status_code}")

            try:
                data = resp.json()[f"{src}{dst}"]
                quote = Quote(sell=float(data["ask"]), buy=float(data["bid"]), date=data["create_date"])
            except (ValueError, KeyError, TypeError) as e:
                raise UpstreamError(f"resposta inesperada: {e}") from e

            outcome = "ok"
            return quote
        finally:
            self._metric.labels(outcome).observe(time.perf_counter() - start)


# ---------------------------------------------------------------------------
# PTAX do Banco Central (API Olinda, pública e sem cadastro)
# ---------------------------------------------------------------------------
#
# Por que: as APIs de câmbio gratuitas (AwesomeAPI etc.) não respondem para IPs
# da AWS (no EKS a AwesomeAPI devolve 429 para o IP do NAT). O BACEN responde.
#
#   GET {PTAX_URL}/CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,
#       dataFinalCotacao=@dataFinalCotacao)?@moeda='EUR'&@dataInicial='11-13-2026'
#       &@dataFinalCotacao='11-20-2026'&$format=json&$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao
#   -> {"value": [{"cotacaoCompra": 6.3702, "cotacaoVenda": 6.372,
#                  "dataHoraCotacao": "2026-11-18 13:08:44.271245"}, ...]}
#
# As linhas vêm em ordem de tempo; usamos a última (rows[-1]). A janela de 7
# dias cobre fins de semana e feriados. A PTAX só cota moeda -> BRL; BRL vale 1
# e qualquer par sai pela taxa cruzada via BRL:
#     sell = venda(src) / compra(dst)     buy = compra(src) / venda(dst)
# Moeda que o BACEN não conhece devolve "value": [] (200) -> PairNotFound (404).

PTAX_CURRENCIES = frozenset({"AUD", "CAD", "CHF", "DKK", "EUR", "GBP", "JPY", "NOK", "SEK", "USD"})
PTAX_WINDOW_DAYS = 7


class PtaxProvider:
    def __init__(self, client: httpx.AsyncClient, base_url: str, today=None,
                 metric: Histogram = UPSTREAM_SECONDS):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        self._metric = metric
        self._currencies: list[dict] | None = None

    async def fetch(self, src: str, dst: str) -> Quote:
        # atalho: moeda fora da lista da PTAX nem chama o BACEN
        for cur in (src, dst):
            if cur != "BRL" and cur not in PTAX_CURRENCIES:
                raise PairNotFound(f"{src}-{dst}")
        c_src, v_src, d_src = await self._rate(src)
        c_dst, v_dst, d_dst = await self._rate(dst)
        dates = [d for d in (d_src, d_dst) if d]
        return Quote(sell=round(v_src / c_dst, 6), buy=round(c_src / v_dst, 6), date=min(dates))

    async def _rate(self, cur: str) -> tuple[float, float, str | None]:
        """(compra, venda, data) da moeda em BRL; BRL vale 1."""
        if cur == "BRL":
            return 1.0, 1.0, None
        end = self._today()
        fmt = "%m-%d-%Y"
        url = (
            f"{self._base_url}/CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,"
            f"dataFinalCotacao=@dataFinalCotacao)?@moeda='{cur}'"
            f"&@dataInicial='{(end - timedelta(days=PTAX_WINDOW_DAYS)).strftime(fmt)}'"
            f"&@dataFinalCotacao='{end.strftime(fmt)}'"
            f"&$format=json&$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao"
        )
        start = time.perf_counter()
        outcome = "error"
        try:
            rows = (await self._get_json(url))["value"]
            if not isinstance(rows, list):
                raise UpstreamError("PTAX resposta inesperada: value não é lista")
            if not rows:
                outcome = "not_found"
                raise PairNotFound(cur)
            try:
                last = rows[-1]
                rate = float(last["cotacaoCompra"]), float(last["cotacaoVenda"]), str(last["dataHoraCotacao"])[:19]
            except (ValueError, KeyError, TypeError) as e:
                raise UpstreamError(f"PTAX resposta inesperada: {e!r}") from e
            outcome = "ok"
            return rate
        finally:
            self._metric.labels(outcome).observe(time.perf_counter() - start)

    async def currencies(self) -> list[dict]:
        """Moedas que a PTAX cota ([{symbol, name}]). Guardado depois do 1º sucesso."""
        if self._currencies is None:
            data = await self._get_json(f"{self._base_url}/Moedas?$format=json&$select=simbolo,nomeFormatado")
            try:
                self._currencies = [{"symbol": m["simbolo"], "name": m["nomeFormatado"]} for m in data["value"]]
            except (KeyError, TypeError) as e:
                raise UpstreamError(f"PTAX resposta inesperada: {e!r}") from e
        return self._currencies

    async def _get_json(self, url: str):
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as e:
            raise UpstreamError(str(e)) from e
        if resp.status_code != 200:
            raise UpstreamError(f"PTAX status {resp.status_code}")
        try:
            data = resp.json()
            data["value"]
        except (ValueError, KeyError, TypeError) as e:
            raise UpstreamError(f"PTAX resposta inesperada: {e!r}") from e
        return data


# ---------------------------------------------------------------------------
# Provedor composto (só no modo EXCHANGE_PROVIDER=awesomeapi): AwesomeAPI e,
# se ela falhar, PTAX
# ---------------------------------------------------------------------------

FALLBACK_TOTAL = Counter(
    "exchange_fallback_total",
    "Vezes em que o provedor principal falhou e o fallback foi usado",
    ["provider", "outcome"],  # outcome: ok | error | unsupported
)


class FallbackProvider:
    """Tenta o principal; só em UpstreamError (timeout, 5xx, 429...) usa o fallback.

    PairNotFound do principal não cai no fallback: a AwesomeAPI cobre mais moedas
    que a PTAX, então se ela diz que o par não existe a PTAX não vai ter.
    Se o fallback não suporta o par, o erro original (UpstreamError -> 503) é
    mantido, porque o par provavelmente existe e só o principal está fora.
    """

    def __init__(self, primary, fallback, name: str):
        self._primary = primary
        self._fallback = fallback
        self._name = name

    async def fetch(self, src: str, dst: str) -> Quote:
        try:
            return await self._primary.fetch(src, dst)
        except UpstreamError as primary_error:
            try:
                quote = await self._fallback.fetch(src, dst)
            except PairNotFound:
                FALLBACK_TOTAL.labels(self._name, "unsupported").inc()
                raise primary_error
            except UpstreamError:
                FALLBACK_TOTAL.labels(self._name, "error").inc()
                raise
            FALLBACK_TOTAL.labels(self._name, "ok").inc()
            return quote
