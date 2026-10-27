"""Revisão de 27/10/2026, onda 2: sincronização e redundância (o que não está em test_espelho/test_failover)."""
from datetime import datetime, timedelta, timezone

import pytest
from psycopg2.extras import Json

from app import create_app, db
from app.config import TestConfig
from app.espelho import service
from tests.conftest import DEVICE_ID, access_log, dao
from worker import espelho as wespelho
from worker import main as wmain
from worker import sync


class NuvemConfig(TestConfig):
    MODO = "nuvem"


def _token(uid, token):
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"monitor_token": token}), uid))


def test_s7_token_da_catraca_e_por_escola(seed):
    """Na nuvem o token global não vale; o de uma escola não vale pra catraca de outra."""
    nuvem = create_app(NuvemConfig).test_client()
    uid = seed["unidade"]["id"]
    _token(uid, "tok-recreio")
    outra = db.execute("INSERT INTO unidades (empresa_id, nome, config) VALUES (%s,'Itaguaí','{\"monitor_token\": \"tok-itaguai\"}') RETURNING id",
                       (seed["empresa"],), returning=True)["id"]
    log = access_log(1, 1, 1)
    assert dao(nuvem, log, token="tok-teste").status_code == 403  # MONITOR_TOKEN global
    assert dao(nuvem, log, token="tok-itaguai").status_code == 403  # catraca do Recreio com token de Itaguaí
    assert dao(nuvem, log, token="tok-recreio").status_code == 200
    assert outra
    db.gucs, db.contexto.unidade_id = {}, None


def test_monitor_token_gerado_para_escola_nova(cliente, seed):
    from app.espelho.service import garantir_monitor_token

    t1 = garantir_monitor_token(seed["unidade"]["id"])
    assert len(t1) == 32 and garantir_monitor_token(seed["unidade"]["id"]) == t1  # não troca o que existe


def test_catraca_aponta_pro_nome_da_escola(seed, monkeypatch):
    uid = seed["unidade"]["id"]
    monkeypatch.setenv("BASE_URL", "http://192.168.0.5:8000")
    monkeypatch.setenv("MONITOR_TOKEN", "tok-env")
    d = seed["dispositivo"]
    assert sync.destino_da_catraca(d) == ("192.168.0.5", 8000, "tok-env")
    _token(uid, "tok-recreio")
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"dns_nome": f"catraca-u{uid}.nexosix.com.br"}), uid))
    assert sync.destino_da_catraca(d) == (f"catraca-u{uid}.nexosix.com.br", 80, "tok-recreio")


def test_e16_nao_acerta_a_catraca_com_relogio_errado(sincronizado):
    db.execute("UPDATE espelho_estado SET desvio_relogio_seg = 600 WHERE id = 1")
    from app.dispositivos import service as dispositivos

    dispositivos.enfileirar(sincronizado["dispositivo"]["id"], "set_clock", payload={"fuso": "America/Sao_Paulo"})
    sync.processar_fila()
    item = db.query("SELECT status, erro FROM fila_sync WHERE tipo = 'set_clock'", one=True)
    assert item["status"] == "erro" and "relógio" in item["erro"]
    db.execute("UPDATE espelho_estado SET desvio_relogio_seg = 3 WHERE id = 1")
    dispositivos.enfileirar(sincronizado["dispositivo"]["id"], "set_clock", payload={"fuso": "America/Sao_Paulo"})
    sync.processar_fila()
    assert db.query("SELECT count(*) AS n FROM fila_sync WHERE tipo = 'set_clock' AND status = 'concluido'", one=True)["n"] == 1


def test_e1_falha_ao_puxar_nao_para_a_catraca(seed, monkeypatch):
    chamadas = []
    monkeypatch.setattr(wespelho, "garantir_carga_inicial", lambda: True)
    monkeypatch.setattr(wespelho, "puxar", lambda: (_ for _ in ()).throw(RuntimeError("nuvem quebrou")))
    monkeypatch.setattr(wmain, "rodada_catraca", lambda a, c: chamadas.append("catraca"))
    monkeypatch.setattr(wespelho, "empurrar", lambda: chamadas.append("empurrar") or 0)
    wmain.rodada_agente({}, {})
    assert chamadas == ["catraca", "empurrar"]


def test_e6_ping_mede_o_relogio_e_o_site(seed, monkeypatch):
    enviados = []
    adiantada = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(service, "ping", lambda nuvem, token, dados, post=None: enviados.append(dados) or {"agora": adiantada, "schema_nuvem": service.versao_schema()})
    monkeypatch.setattr(wespelho, "web_ok", lambda: False)
    monkeypatch.setenv("NUVEM_URL", "https://n")
    monkeypatch.setenv("ESPELHO_TOKEN", "t")
    wespelho.ping()
    assert enviados[0]["web_ok"] is False and enviados[0]["schema"].startswith("009")
    assert 290 <= db.query("SELECT desvio_relogio_seg FROM espelho_estado", one=True)["desvio_relogio_seg"] <= 300


def test_e10_reinstalar_nao_refaz_a_carga(app, seed):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["configurar-local", "--unidade-id", str(seed["unidade"]["id"])]).exit_code == 0
    agente_id = service.estado_local()["agente_id"]
    assert agente_id
    db.execute("UPDATE espelho_estado SET carga_em = now(), cursor_xid = '123', cursor_nuvem = 7")
    assert runner.invoke(args=["configurar-local", "--unidade-id", str(seed["unidade"]["id"])]).exit_code == 0
    est = service.estado_local()
    assert est["carga_em"] and est["cursor_xid"] == "123" and est["agente_id"] == agente_id
    r = runner.invoke(args=["espelho-enviar"])
    assert r.exit_code == 0 and "nada a enviar" in r.output
    db.gucs, db.contexto.unidade_id = {}, None


def test_carga_inicial_baixa_fotos_em_paginas(seed, monkeypatch):
    """E3/E15: cadastro e fotos separados; a carga só conta como feita no fim."""
    un = seed["unidade"]
    snap = service.snapshot(un)
    fotos = service.fotos(un, limite=500)

    class R:
        def __init__(self, corpo):
            self.ok, self.status_code, self._c = True, 200, corpo

        def json(self):
            return self._c

    paginas = []

    def get(url, headers, timeout, params=None):
        assert headers["X-Espelho-Token"] == "t" and "/t/" not in url  # token só no cabeçalho
        if url.endswith("/snapshot"):
            return R({"ok": True, **snap})
        apos = params["apos"]
        pagina = [f for f in fotos if f["chave"]["pessoa_id"] > apos][:2]
        paginas.append(len(pagina))
        return R({"ok": True, "itens": pagina})

    db.execute("UPDATE espelho_estado SET carga_em = NULL")
    db.execute("DELETE FROM pessoa_fotos")
    service.carga_inicial("https://n", "t", un["id"], get=get)
    assert paginas == [2, 1, 0]
    assert db.query("SELECT count(*) AS n FROM pessoa_fotos", one=True)["n"] == 3
    est = service.estado_local()
    assert est["carga_em"] and est["cursor_xid"] == snap["cursor"]["xid"]
    assert not service.precisa_carga()


def test_recarga_pedida_pela_nuvem_refaz_a_carga(seed, monkeypatch):
    db.execute("UPDATE espelho_estado SET carga_em = now()")

    def puxar(*a, **k):
        raise service.RecargaNecessaria("velho")

    monkeypatch.setattr(service, "puxar", puxar)
    wespelho.puxar()
    assert service.precisa_carga()


@pytest.mark.parametrize("ip,ok", [("192.168.0.5", True), ("10.1.2.3", True), ("172.16.0.9", True), ("8.8.8.8", False),
                                   ("127.0.0.1", False), ("169.254.1.1", False), ("lixo", False)])
def test_ip_privado(ip, ok):
    assert service.ip_privado(ip) is ok


def test_evento_duplicado_entre_nuvem_e_agente_nao_quebra(seed):
    """Failover: a nuvem recebeu o log pela catraca; depois o agente coleta o mesmo log."""
    un = seed["unidade"]
    base = {"unidade_id": un["id"], "dispositivo_id": seed["dispositivo"]["id"], "pessoa_id": seed["maria"]["pessoa_id"], "tipo": "ENTRADA",
            "sentido": "entrada", "ocorrido_em": "2026-10-27T10:00:00+00:00", "origem": "dispositivo", "log_id_dispositivo": 55, "detalhes": {}}
    db.execute("INSERT INTO eventos (id, unidade_id, dispositivo_id, pessoa_id, tipo, sentido, ocorrido_em, log_id_dispositivo) VALUES (5, %s,%s,%s,'ENTRADA','entrada', now(), 55)",
               (un["id"], seed["dispositivo"]["id"], seed["maria"]["pessoa_id"]))
    aplicados, rejeitados = service.aplicar_lote(un, [{"tabela": "eventos", "operacao": "upsert", "chave": {"id": 10_000_009}, "dados": {**base, "id": 10_000_009}}])
    assert (aplicados, rejeitados) == (1, 0)
    assert db.query("SELECT count(*) AS n FROM espelho_quarentena", one=True)["n"] == 0
    assert DEVICE_ID
