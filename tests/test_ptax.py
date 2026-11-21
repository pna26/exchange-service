"""Testes do fallback PTAX (Banco Central) com as duas APIs simuladas, sem rede."""

import asyncio
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.provider import (
    AwesomeApiProvider,
    FallbackProvider,
    PairNotFound,
    PtaxProvider,
    UpstreamError,
)

AWESOME = "https://awesome.test/json/last"
PTAX = "https://ptax.test/odata"

# formato real do BACEN (CotacaoMoedaPeriodo), recortado
PTAX_EUR = {
    "value": [
        {"cotacaoCompra": 6.3702, "cotacaoVenda": 6.372, "dataHoraCotacao": "2026-11-18 13:08:44.271245", "tipoBoletim": "Fechamento"},
        {"cotacaoCompra": 6.4767, "cotacaoVenda": 6.4779, "dataHoraCotacao": "2026-11-19 10:11:10.714595", "tipoBoletim": "Abertura"},
        {"cotacaoCompra": 6.4659, "cotacaoVenda": 6.4672, "dataHoraCotacao": "2026-11-19 13:04:18.325373", "tipoBoletim": "Fechamento"},
        {"cotacaoCompra": 6.5, "cotacaoVenda": 6.51, "dataHoraCotacao": "2026-11-20 10:10:00.000000", "tipoBoletim": "Abertura"},
    ]
}
# CotacaoDolarPeriodo não tem tipoBoletim
PTAX_USD = {
    "value": [
        {"cotacaoCompra": 5.8096, "cotacaoVenda": 5.8102, "dataHoraCotacao": "2026-11-18 13:08:44.271245"},
        {"cotacaoCompra": 5.8883, "cotacaoVenda": 5.8889, "dataHoraCotacao": "2026-11-19 13:04:18.325373"},
    ]
}


class FakeApis:
    """Simula AwesomeAPI e PTAX no mesmo transport e conta as chamadas de cada uma."""

    def __init__(self, awesome_status=429, ptax_status=200, ptax_bodies=None, ptax_fail=False):
        self.awesome_status = awesome_status
        self.ptax_status = ptax_status
        self.ptax_bodies = {"USD": PTAX_USD, "EUR": PTAX_EUR} if ptax_bodies is None else ptax_bodies
        self.ptax_fail = ptax_fail
        self.awesome_calls = 0
        self.ptax_calls = 0
        self.ptax_urls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(PTAX):
            self.ptax_calls += 1
            self.ptax_urls.append(url)
            if self.ptax_fail:
                raise httpx.ConnectTimeout("timeout", request=request)
            if self.ptax_status != 200:
                return httpx.Response(self.ptax_status)
            cur = next((c for c in self.ptax_bodies if f"@moeda='{c}'" in url or f"%27{c}%27" in url), None)
            return httpx.Response(200, json=self.ptax_bodies.get(cur, {"value": []}))
        self.awesome_calls += 1
        if self.awesome_status != 200:
            return httpx.Response(self.awesome_status, json={"status": self.awesome_status})
        return httpx.Response(200, json={"USDBRL": {"bid": "5.3912", "ask": "5.3942", "create_date": "2026-11-05 10:00:00"}})


def run(coro):
    return asyncio.run(coro)


async def _ptax_fetch(api, src, dst):
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as c:
        return await PtaxProvider(c, PTAX, today=lambda: date(2026, 11, 20)).fetch(src, dst)


async def _fallback_fetch(api, src, dst):
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as c:
        provider = FallbackProvider(AwesomeApiProvider(c, AWESOME), PtaxProvider(c, PTAX), name="ptax")
        return await provider.fetch(src, dst)


# --- PtaxProvider -----------------------------------------------------------

def test_ptax_parse_usd_brl():
    api = FakeApis()
    q = run(_ptax_fetch(api, "USD", "BRL"))
    assert (q.sell, q.buy, q.date) == (5.8889, 5.8883, "2026-11-19 13:04:18")
    # janela de 7 dias para trás, datas no formato MM-DD-YYYY do OData
    assert "11-13-2026" in api.ptax_urls[0] and "11-20-2026" in api.ptax_urls[0]


def test_ptax_prefere_ultimo_fechamento():
    q = run(_ptax_fetch(FakeApis(), "EUR", "BRL"))
    # a abertura de 20/11 é mais nova, mas o fechamento de 19/11 é a PTAX do dia
    assert (q.sell, q.buy, q.date) == (6.4672, 6.4659, "2026-11-19 13:04:18")


def test_ptax_sem_fechamento_pega_a_ultima():
    body = {"value": [PTAX_EUR["value"][3], PTAX_EUR["value"][1]]}  # só aberturas, fora de ordem
    q = run(_ptax_fetch(FakeApis(ptax_bodies={"EUR": body}), "EUR", "BRL"))
    assert q.sell == 6.51


def test_ptax_lista_vazia_e_upstream_error():
    with pytest.raises(UpstreamError):
        run(_ptax_fetch(FakeApis(ptax_bodies={"USD": {"value": []}}), "USD", "BRL"))


def test_ptax_resposta_invalida_e_upstream_error():
    with pytest.raises(UpstreamError):
        run(_ptax_fetch(FakeApis(ptax_bodies={"USD": {"erro": 1}}), "USD", "BRL"))


def test_ptax_brl_para_moeda_invertido():
    q = run(_ptax_fetch(FakeApis(), "BRL", "USD"))
    assert q.sell == round(1 / 5.8883, 6)
    assert q.buy == round(1 / 5.8889, 6)
    assert q.sell >= q.buy


def test_ptax_cruzado_via_brl():
    q = run(_ptax_fetch(FakeApis(), "EUR", "USD"))
    assert q.sell == round(6.4672 / 5.8883, 6)
    assert q.buy == round(6.4659 / 5.8889, 6)
    assert q.sell >= q.buy


def test_ptax_moeda_desconhecida_nao_chama_api():
    api = FakeApis()
    with pytest.raises(PairNotFound):
        run(_ptax_fetch(api, "BTC", "BRL"))
    assert api.ptax_calls == 0


# --- FallbackProvider --------------------------------------------------------

def test_fallback_acionado_no_429():
    api = FakeApis(awesome_status=429)
    q = run(_fallback_fetch(api, "USD", "BRL"))
    assert q.sell == 5.8889
    assert api.awesome_calls == 1 and api.ptax_calls == 1


def test_fallback_nao_acionado_no_404():
    api = FakeApis(awesome_status=404)
    with pytest.raises(PairNotFound):
        run(_fallback_fetch(api, "USD", "BRL"))
    assert api.ptax_calls == 0


def test_fallback_nao_acionado_quando_principal_responde():
    api = FakeApis(awesome_status=200)
    assert run(_fallback_fetch(api, "USD", "BRL")).sell == 5.3942
    assert api.ptax_calls == 0


def test_fallback_par_nao_suportado_mantem_erro_original():
    # AwesomeAPI fora e PTAX não cota BTC: continua 503 (UpstreamError), não 404
    with pytest.raises(UpstreamError):
        run(_fallback_fetch(FakeApis(awesome_status=503), "BTC", "BRL"))


# --- rota ----------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EXCHANGE_API_URL", AWESOME)
    monkeypatch.setenv("PTAX_BASE_URL", PTAX)
    return monkeypatch


def get(client, path):
    return client.get(path, headers={"id-account": "42"})


def test_rota_usa_ptax_quando_awesome_da_429(env):
    api = FakeApis(awesome_status=429)
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        r = get(c, "/exchange/USD/BRL")
    assert r.status_code == 200
    assert r.json() == {"sell": 5.8889, "buy": 5.8883, "date": "2026-11-19 13:04:18", "id-account": "42"}


def test_rota_fallback_desligado(env):
    env.setenv("EXCHANGE_FALLBACK", "none")
    api = FakeApis(awesome_status=429)
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 503
    assert api.ptax_calls == 0


def test_rota_ambos_falham_503_e_cache_continua(env):
    api = FakeApis(awesome_status=200, ptax_fail=True)
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 200  # popula o cache
        api.awesome_status = 429
        # dentro do TTL: cache responde, nenhuma API é chamada
        for _ in range(5):
            assert get(c, "/exchange/USD/BRL").json()["sell"] == 5.3942
        assert api.awesome_calls == 1 and api.ptax_calls == 0
        # par sem cache e as duas APIs fora: 503
        assert get(c, "/exchange/EUR/BRL").status_code == 503
        assert api.ptax_calls == 1
        text = c.get("/metrics").text
    assert 'exchange_fallback_total{outcome="error",provider="ptax"}' in text
