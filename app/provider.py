"""Clientes das APIs de câmbio: AwesomeAPI (principal) e PTAX do BACEN (fallback).

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

UPSTREAM_SECONDS = Histogram(
    "exchange_upstream_request_seconds",
    "Tempo das chamadas à API de câmbio externa",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5),
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
    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str | None = None):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token

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
            UPSTREAM_SECONDS.labels(outcome).observe(time.perf_counter() - start)


# ---------------------------------------------------------------------------
# Fallback: PTAX do Banco Central (API Olinda, pública e sem cadastro)
# ---------------------------------------------------------------------------
#
# Por que: no EKS a AwesomeAPI responde 429 para o IP do NAT da AWS (o plano
# gratuito limita IPs de nuvem), e o exchange devolvia 503. O BACEN não tem
# esse limite.
#
#   GET {PTAX_BASE_URL}/CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,
#       dataFinalCotacao=@dataFinalCotacao)?@moeda='EUR'&@dataInicial='11-13-2026'
#       &@dataFinalCotacao='11-20-2026'&$format=json
#   -> {"value": [{"cotacaoCompra": 6.3702, "cotacaoVenda": 6.372,
#                  "dataHoraCotacao": "2026-11-18 13:08:44.271245",
#                  "tipoBoletim": "Fechamento", ...}, ...]}
#
# A PTAX só cota moeda -> BRL e só para as 10 moedas abaixo (endpoint Moedas).
# Moeda desconhecida devolve "value": [] com status 200, por isso a lista fixa:
# moeda fora dela é PairNotFound sem chamada; lista vazia para moeda conhecida
# é UpstreamError (o BACEN não publicou nada na janela).

PTAX_SECONDS = Histogram(
    "exchange_ptax_request_seconds",
    "Tempo das chamadas à API PTAX do Banco Central (fallback)",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5),
)

PTAX_CURRENCIES = frozenset({"AUD", "CAD", "CHF", "DKK", "EUR", "GBP", "JPY", "NOK", "SEK", "USD"})
PTAX_WINDOW_DAYS = 7  # fins de semana e feriados não têm cotação


class PtaxProvider:
    """Cotação PTAX. Suporta X->BRL, BRL->X (invertido) e X->Y (cruzado via BRL)."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, today=None):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    async def fetch(self, src: str, dst: str) -> Quote:
        for cur in (src, dst):
            if cur != "BRL" and cur not in PTAX_CURRENCIES:
                raise PairNotFound(f"{src}-{dst}")

        if dst == "BRL":
            return await self._to_brl(src)
        if src == "BRL":
            q = await self._to_brl(dst)
            # quem vende BRL e recebe X: 1/compra; manter sell >= buy
            return Quote(sell=round(1 / q.buy, 6), buy=round(1 / q.sell, 6), date=q.date)
        # cruzado: X->Y = (X->BRL) / (Y->BRL); o spread dos dois lados se soma
        x, y = await self._to_brl(src), await self._to_brl(dst)
        return Quote(sell=round(x.sell / y.buy, 6), buy=round(x.buy / y.sell, 6), date=min(x.date, y.date))

    async def _to_brl(self, cur: str) -> Quote:
        end = self._today()
        start_day = end - timedelta(days=PTAX_WINDOW_DAYS)
        fmt = "%m-%d-%Y"
        url = (
            f"{self._base_url}/CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,"
            f"dataFinalCotacao=@dataFinalCotacao)?@moeda='{cur}'"
            f"&@dataInicial='{start_day.strftime(fmt)}'&@dataFinalCotacao='{end.strftime(fmt)}'&$format=json"
        )
        start = time.perf_counter()
        outcome = "error"
        try:
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as e:
                raise UpstreamError(str(e)) from e
            if resp.status_code != 200:
                raise UpstreamError(f"PTAX status {resp.status_code}")
            try:
                quote = _pick_ptax(resp.json()["value"])
            except (ValueError, KeyError, TypeError, IndexError) as e:
                raise UpstreamError(f"PTAX resposta inesperada: {e!r}") from e
            outcome = "ok"
            return quote
        finally:
            PTAX_SECONDS.labels(outcome).observe(time.perf_counter() - start)


def _pick_ptax(rows: list[dict]) -> Quote:
    """Último boletim de Fechamento da janela; sem ele, a última cotação da lista."""
    if not rows:
        raise IndexError("nenhuma cotação PTAX na janela")
    closing = [r for r in rows if r.get("tipoBoletim") == "Fechamento"]
    row = max(closing or rows, key=lambda r: r["dataHoraCotacao"])
    return Quote(
        sell=float(row["cotacaoVenda"]),
        buy=float(row["cotacaoCompra"]),
        date=str(row["dataHoraCotacao"])[:19],
    )


# ---------------------------------------------------------------------------
# Provedor composto: AwesomeAPI e, se ela falhar, PTAX
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
