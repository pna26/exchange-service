"""Testes da PTAX (provedor padrão) e do modo awesomeapi com fallback, sem rede."""

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

# formato real do BACEN (CotacaoMoedaPeriodo com $select), linhas em ordem de tempo
PTAX_USD = {
    "value": [
        {"cotacaoCompra": 5.8096, "cotacaoVenda": 5.8102, "dataHoraCotacao": "2026-11-18 13:08:44.271245"},
        {"cotacaoCompra": 5.8883, "cotacaoVenda": 5.8889, "dataHoraCotacao": "2026-11-19 13:04:18.325373"},
    ]
}
PTAX_EUR = {
    "value": [
        {"cotacaoCompra": 6.3702, "cotacaoVenda": 6.372, "dataHoraCotacao": "2026-11-18 13:08:44.271245"},
        {"cotacaoCompra": 6.4659, "cotacaoVenda": 6.4672, "dataHoraCotacao": "2026-11-19 13:04:18.325373"},
        {"cotacaoCompra": 6.5, "cotacaoVenda": 6.51, "dataHoraCotacao": "2026-11-20 10:10:00.000000"},
    ]
}
PTAX_MOEDAS = {
    "value": [
        {"simbolo": "EUR", "nomeFormatado": "Euro"},
        {"simbolo": "USD", "nomeFormatado": "Dólar dos Estados Unidos"},
    ]
}


class FakeApis:
    """Simula PTAX e AwesomeAPI no mesmo transport e conta as chamadas de cada uma."""

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
            if "/Moedas" in url:
                return httpx.Response(200, json=PTAX_MOEDAS)
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

def test_ptax_moeda_para_brl():
    api = FakeApis()
    q = run(_ptax_fetch(api, "USD", "BRL"))
    assert (q.sell, q.buy, q.date) == (5.8889, 5.8883, "2026-11-19 13:04:18")
    # janela de 7 dias para trás, datas MM-DD-YYYY do OData, só os campos usados
    url = api.ptax_urls[0]
    assert "11-13-2026" in url and "11-20-2026" in url
    assert "$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao" in url
    assert api.ptax_calls == 1  # BRL vale 1, não chama o BACEN


def test_ptax_usa_o_ultimo_boletim():
    q = run(_ptax_fetch(FakeApis(), "EUR", "BRL"))
    assert (q.sell, q.buy, q.date) == (6.51, 6.5, "2026-11-20 10:10:00")  # rows[-1]


def test_ptax_brl_para_moeda():
    q = run(_ptax_fetch(FakeApis(), "BRL", "USD"))
    assert q.sell == round(1 / 5.8883, 6)
    assert q.buy == round(1 / 5.8889, 6)
    assert q.sell >= q.buy


def test_ptax_cruzado_usd_eur():
    q = run(_ptax_fetch(FakeApis(), "USD", "EUR"))
    # fórmula do roteiro: sell = venda(src) / compra(dst), buy = compra(src) / venda(dst)
    assert q.sell == round(5.8889 / 6.5, 6)
    assert q.buy == round(5.8883 / 6.51, 6)
    assert q.sell >= q.buy
    assert q.date == "2026-11-19 13:04:18"  # a mais antiga das duas


def test_ptax_lista_vazia_e_moeda_desconhecida():
    with pytest.raises(PairNotFound):
        run(_ptax_fetch(FakeApis(ptax_bodies={"USD": {"value": []}}), "USD", "BRL"))


def test_ptax_moeda_fora_da_lista_nao_chama_api():
    api = FakeApis()
    with pytest.raises(PairNotFound):
        run(_ptax_fetch(api, "BTC", "BRL"))
    assert api.ptax_calls == 0


def test_ptax_resposta_invalida_e_upstream_error():
    with pytest.raises(UpstreamError):
        run(_ptax_fetch(FakeApis(ptax_bodies={"USD": {"erro": 1}}), "USD", "BRL"))


# --- rota no modo padrão (PTAX) ----------------------------------------------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("EXCHANGE_PROVIDER", raising=False)
    monkeypatch.setenv("PTAX_URL", PTAX)
    monkeypatch.setenv("EXCHANGE_API_URL", AWESOME)
    return monkeypatch


def get(client, path):
    return client.get(path, headers={"id-account": "42"})


def test_rota_ptax_padrao_e_x_cache_miss_depois_hit(env):
    api = FakeApis()
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        r1 = get(c, "/exchange/USD/BRL")
        r2 = get(c, "/exchange/USD/BRL")
    assert r1.status_code == 200
    assert r1.json() == {"sell": 5.8889, "buy": 5.8883, "date": "2026-11-19 13:04:18", "id-account": "42"}
    assert r1.headers["X-Cache"] == "miss"
    assert r2.headers["X-Cache"] == "hit"
    assert api.ptax_calls == 1 and api.awesome_calls == 0  # AwesomeAPI nem é chamada


def test_rota_alias_exchanges(env):
    with TestClient(create_app(transport=httpx.MockTransport(FakeApis()))) as c:
        r = get(c, "/exchanges/USD/EUR")
    assert r.status_code == 200
    assert r.json()["sell"] == round(5.8889 / 6.5, 6)


def test_rota_moeda_desconhecida_404(env):
    api = FakeApis(ptax_bodies={})  # BACEN devolve "value": [] para tudo
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 404
        assert get(c, "/exchange/XYZ/BRL").status_code == 404


def test_rota_status_400_e_401(env):
    with TestClient(create_app(transport=httpx.MockTransport(FakeApis()))) as c:
        assert get(c, "/exchange/US/BRL").status_code == 400
        assert c.get("/exchange/USD/BRL").status_code == 401


@pytest.mark.parametrize("falha", [{"ptax_status": 500}, {"ptax_fail": True}])
def test_rota_ptax_fora_sem_cache_503(env, falha):
    with TestClient(create_app(transport=httpx.MockTransport(FakeApis(**falha)))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 503


def test_rota_ptax_fora_com_cache_vencido_stale(env):
    env.setenv("CACHE_TTL", "0")  # toda cotação já nasce vencida
    api = FakeApis()
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        assert get(c, "/exchange/USD/BRL").headers["X-Cache"] == "miss"
        api.ptax_fail = True
        r = get(c, "/exchange/USD/BRL")
        text = c.get("/metrics").text
    assert r.status_code == 200
    assert r.headers["X-Cache"] == "stale"
    assert r.json()["sell"] == 5.8889
    assert 'exchange_quote_cache_total{result="stale"}' in text
    # no modo ptax a métrica do "API externa" do Grafana mede a PTAX
    assert 'exchange_upstream_request_seconds_count{outcome="ok"}' in text


def test_rota_currencies(env):
    with TestClient(create_app(transport=httpx.MockTransport(FakeApis()))) as c:
        r = get(c, "/exchange/currencies")
    assert r.status_code == 200
    assert r.json() == [{"symbol": "EUR", "name": "Euro"}, {"symbol": "USD", "name": "Dólar dos Estados Unidos"}]


def test_rota_currencies_ptax_fora_503(env):
    with TestClient(create_app(transport=httpx.MockTransport(FakeApis(ptax_status=502)))) as c:
        assert get(c, "/exchange/currencies").status_code == 503


# --- modo awesomeapi com fallback PTAX ---------------------------------------

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


def test_rota_awesomeapi_usa_ptax_no_429(env):
    env.setenv("EXCHANGE_PROVIDER", "awesomeapi")
    api = FakeApis(awesome_status=429)
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        r = get(c, "/exchange/USD/BRL")
    assert r.status_code == 200
    assert r.json()["sell"] == 5.8889


def test_rota_awesomeapi_fallback_desligado(env):
    env.setenv("EXCHANGE_PROVIDER", "awesomeapi")
    env.setenv("EXCHANGE_FALLBACK", "none")
    api = FakeApis(awesome_status=429)
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 503
    assert api.ptax_calls == 0


def test_rota_awesomeapi_ambos_falham_503_e_cache_continua(env):
    env.setenv("EXCHANGE_PROVIDER", "awesomeapi")
    api = FakeApis(awesome_status=200, ptax_fail=True)
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 200  # popula o cache
        api.awesome_status = 429
        for _ in range(5):
            assert get(c, "/exchange/USD/BRL").json()["sell"] == 5.3942
        assert api.awesome_calls == 1 and api.ptax_calls == 0
        assert get(c, "/exchange/EUR/BRL").status_code == 503
        assert api.ptax_calls == 1
        text = c.get("/metrics").text
    assert 'exchange_fallback_total{outcome="error",provider="ptax"}' in text
