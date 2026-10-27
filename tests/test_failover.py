"""Vigia da nuvem: agente mudo → DNS da catraca aponta pra nuvem; agente volta → DNS volta."""
import os
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json

from app import db
from app.espelho import failover, service
from app.eventos import tipos
from tests.conftest import DEVICE_ID, access_log, dao


def _cfg(unidade_id, **campos):
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json(campos), unidade_id))
    return db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)


def test_vigia_ativa_e_encerra_pelo_silencio_do_agente(seed, monkeypatch):
    monkeypatch.setenv("FAILOVER_NUVEM_ALVO", "nexosix-acesso.up.railway.app")
    uid = seed["unidade"]["id"]
    dns = failover.DnsNulo()
    # sem dns_nome: nada acontece mesmo com agente mudo
    assert failover.verificar(dns, apos_seg=60) == []
    _cfg(uid, dns_nome="catraca-recreio.nexosix.com.br")
    # agente nunca falou → assume
    mudancas = failover.verificar(dns, apos_seg=60)
    assert [m[1] for m in mudancas] == ["ativado"]
    assert dns.chamadas[-1] == ("catraca-recreio.nexosix.com.br", "nexosix-acesso.up.railway.app", "CNAME")
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    assert failover.em_failover(u) and u["config"]["failover"]["desde"]
    ev = db.query("SELECT * FROM eventos WHERE tipo = %s", (tipos.FAILOVER_NUVEM,), one=True)
    assert ev and ev["detalhes"]["dns"] == "catraca-recreio.nexosix.com.br"
    # continua mudo → não repete
    assert failover.verificar(dns, apos_seg=60) == []
    # agente deu sinal (com IP da LAN) → devolve
    service.registrar_ping(u, {"versao": "1", "ip_lan": "192.168.0.50", "base_url": "http://catraca-recreio.nexosix.com.br"})
    mudancas = failover.verificar(dns, apos_seg=60)
    assert [m[1] for m in mudancas] == ["encerrado"]
    assert dns.chamadas[-1] == ("catraca-recreio.nexosix.com.br", "192.168.0.50", "A")
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    assert not failover.em_failover(u)
    assert db.query("SELECT 1 FROM eventos WHERE tipo = %s", (tipos.FAILOVER_FIM,), one=True)
    # agente some de novo (visto há 10 min) → assume de novo
    antigo = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _cfg(uid, agente_visto_em=antigo)
    assert [m[1] for m in failover.verificar(dns, apos_seg=60)] == ["ativado"]


def test_sem_ip_da_lan_nao_devolve(seed, monkeypatch):
    monkeypatch.setenv("FAILOVER_NUVEM_ALVO", "nuvem.exemplo")
    u = _cfg(seed["unidade"]["id"], dns_nome="c.exemplo")
    dns = failover.DnsNulo()
    failover.ativar(u, dns)
    u = _cfg(seed["unidade"]["id"], agente_visto_em=datetime.now(timezone.utc).isoformat())  # ping sem ip_lan
    assert failover.verificar(dns, apos_seg=60) == []
    assert failover.em_failover(db.query("SELECT * FROM unidades WHERE id = %s", (u["id"],), one=True))


def test_dns_falhando_nao_muda_estado(seed, monkeypatch):
    monkeypatch.setenv("FAILOVER_NUVEM_ALVO", "nuvem.exemplo")
    u = _cfg(seed["unidade"]["id"], dns_nome="c.exemplo")

    class DnsQuebrado:
        def apontar(self, *a):
            raise RuntimeError("cloudflare 500")

    assert failover.verificar(DnsQuebrado(), apos_seg=60) == []
    assert not failover.em_failover(db.query("SELECT * FROM unidades WHERE id = %s", (u["id"],), one=True))


def test_eventos_recebidos_pela_nuvem_em_failover_descem_pro_agente(outbox_ligado, cliente, sincronizado):
    """Sem login, a API marca a unidade pelo dispositivo: o agente puxa esses eventos ao voltar."""
    db.execute("DELETE FROM espelho_outbox")
    dao(cliente, access_log(1, sincronizado["user_ids"]["maria"], 1))
    linhas = db.query("SELECT tabela, unidade_id FROM espelho_outbox")
    assert linhas and all(l["unidade_id"] == sincronizado["unidade"]["id"] for l in linhas)
    assert {l["tabela"] for l in linhas} >= {"eventos", "presenca", "fila_notificacao"}


def test_cloudflare_cria_ou_atualiza_registro():
    chamadas = []

    class Resp:
        def __init__(self, corpo):
            self._c = corpo

        def raise_for_status(self):
            pass

        def json(self):
            return self._c

    class Http:
        def get(self, url, headers, params, timeout):
            chamadas.append(("get", params["name"]))
            return Resp({"result": [{"id": "abc"}] if params["name"] == "existe" else []})

        def put(self, url, headers, json, timeout):
            chamadas.append(("put", url.rsplit("/", 1)[1], json))
            return Resp({"success": True})

        def post(self, url, headers, json, timeout):
            chamadas.append(("post", json))
            return Resp({"success": True})

    cf = failover.DnsCloudflare("tok", "zona", http=Http())
    assert cf.apontar("existe", "1.2.3.4", "A")
    assert chamadas[-1][0] == "put" and chamadas[-1][1] == "abc" and chamadas[-1][2]["proxied"] is False
    assert cf.apontar("novo", "nuvem.exemplo", "CNAME")
    assert chamadas[-1][0] == "post" and chamadas[-1][1]["proxied"] is True and chamadas[-1][1]["ttl"] == 60
