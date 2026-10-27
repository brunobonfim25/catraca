"""Vigia da nuvem: agente mudo → DNS da catraca aponta pra nuvem; agente volta → DNS volta."""
import pytest
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json

from app import db
from app.espelho import failover, service
from app.eventos import tipos
from tests.conftest import DEVICE_ID, access_log, dao


def _cfg(unidade_id, **campos):
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json(campos), unidade_id))
    return db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)


@pytest.fixture
def dominio(monkeypatch):
    monkeypatch.setenv("FAILOVER_NUVEM_ALVO", "nexosix-acesso.up.railway.app")
    monkeypatch.setenv("FAILOVER_DOMINIO", "nexosix.com.br")


def _agente(uid, **campos):
    """Ping do agente agora, com o que ele reporta (ip_lan, web_ok…)."""
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    service.registrar_ping(u, {"versao": "2", **campos})
    return db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)


def test_vigia_ativa_e_encerra_pelo_silencio_do_agente(seed, dominio):
    uid = seed["unidade"]["id"]
    dns = failover.DnsNulo()
    # escola sem agente instalado: nada a fazer
    assert failover.verificar(dns, apos_seg=60, volta_seg=0) == []
    _cfg(uid, espelho_token="t")
    # agente nunca falou → assume, com o nome calculado da escola
    mudancas = failover.verificar(dns, apos_seg=60, volta_seg=0)
    assert [m[1] for m in mudancas] == ["ativado"]
    assert dns.chamadas[-1] == (f"catraca-u{uid}.nexosix.com.br", "nexosix-acesso.up.railway.app", "CNAME")
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    assert failover.em_failover(u) and u["config"]["failover"]["desde"] and u["config"]["dns_nome"] == f"catraca-u{uid}.nexosix.com.br"
    ev = db.query("SELECT * FROM eventos WHERE tipo = %s", (tipos.FAILOVER_NUVEM,), one=True)
    assert ev and ev["detalhes"]["dns"] == f"catraca-u{uid}.nexosix.com.br"
    # continua mudo → não repete
    assert failover.verificar(dns, apos_seg=60, volta_seg=0) == []
    # agente deu sinal (com IP da LAN) → devolve
    _agente(uid, ip_lan="192.168.0.50", web_ok=True)
    mudancas = failover.verificar(dns, apos_seg=60, volta_seg=0)
    assert [m[1] for m in mudancas] == ["encerrado"]
    assert dns.chamadas[-1] == (f"catraca-u{uid}.nexosix.com.br", "192.168.0.50", "A")
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    assert not failover.em_failover(u)
    assert db.query("SELECT 1 FROM eventos WHERE tipo = %s", (tipos.FAILOVER_FIM,), one=True)
    # agente some de novo (visto há 10 min) → assume de novo
    antigo = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _cfg(uid, agente_visto_em=antigo)
    assert [m[1] for m in failover.verificar(dns, apos_seg=60, volta_seg=0)] == ["ativado"]


def test_volta_so_depois_de_estavel(seed, dominio):
    """E12: PC que liga e desliga não faz o DNS pular a cada 30 s."""
    uid = seed["unidade"]["id"]
    _cfg(uid, espelho_token="t")
    dns = failover.DnsNulo()
    failover.verificar(dns, apos_seg=60, volta_seg=120)
    _agente(uid, ip_lan="192.168.0.50", web_ok=True)
    assert failover.verificar(dns, apos_seg=60, volta_seg=120) == []  # saudável há 0 s: ainda não
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    assert failover.em_failover(u) and u["config"]["failover"]["saudavel_desde"]
    # 3 min saudável → devolve
    fo = {**u["config"]["failover"], "saudavel_desde": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()}
    _cfg(uid, failover=fo)
    assert [m[1] for m in failover.verificar(dns, apos_seg=60, volta_seg=120)] == ["encerrado"]


def test_site_do_agente_fora_conta_como_fora(seed, dominio):
    """E6: worker dando sinal com o site (o que a catraca chama) caído também é falha."""
    uid = seed["unidade"]["id"]
    _cfg(uid, espelho_token="t")
    dns = failover.DnsNulo()
    _agente(uid, ip_lan="192.168.0.50", web_ok=True)
    assert [m[1] for m in failover.verificar(dns, apos_seg=60, volta_seg=0)] == ["ip"]  # primeira vez: aponta o nome pro PC
    _agente(uid, ip_lan="192.168.0.50", web_ok=False)
    assert failover.verificar(dns, apos_seg=60, volta_seg=0) == []  # acabou de cair: espera
    u = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    ag = {**u["config"]["agente"], "web_falha_desde": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}
    _cfg(uid, agente=ag)
    assert [m[1] for m in failover.verificar(dns, apos_seg=60, volta_seg=0)] == ["ativado"]


def test_dns_acompanha_o_ip_do_pc(seed, dominio):
    """E16: o roteador trocou o IP do PC → o nome passa a apontar pro IP novo."""
    uid = seed["unidade"]["id"]
    _cfg(uid, espelho_token="t")
    dns = failover.DnsNulo()
    _agente(uid, ip_lan="192.168.0.50", web_ok=True)
    failover.verificar(dns, apos_seg=60, volta_seg=0)
    assert failover.verificar(dns, apos_seg=60, volta_seg=0) == []  # mesmo IP: nada
    _agente(uid, ip_lan="192.168.0.77", web_ok=True)
    assert [m[1] for m in failover.verificar(dns, apos_seg=60, volta_seg=0)] == ["ip"]
    assert dns.chamadas[-1] == (f"catraca-u{uid}.nexosix.com.br", "192.168.0.77", "A")
    # IP público reportado pelo agente é ignorado (S6)
    _agente(uid, ip_lan="200.10.10.10", web_ok=True)
    assert failover.verificar(dns, apos_seg=60, volta_seg=0) == []


def test_nome_dns_preso_ao_dominio(seed, dominio):
    """S6: nome fora do domínio da Nexosix (ex.: o site principal) nunca é apontado."""
    assert failover.nome_valido("catraca-u1.nexosix.com.br")
    assert not failover.nome_valido("nexosix.com.br") and not failover.nome_valido("www.outro.com") and not failover.nome_valido("x.nexosix.com.br.evil.com")
    uid = seed["unidade"]["id"]
    u = _cfg(uid, espelho_token="t", dns_nome="www.google.com")
    assert failover.nome_da_unidade(u) == f"catraca-u{uid}.nexosix.com.br"


def test_admin_da_escola_nao_troca_o_nome(cliente, seed, dominio):
    uid = seed["unidade"]["id"]
    cliente.post(f"/unidades/{uid}/config", data={"nome": "Recreio", "fuso": "America/Sao_Paulo", "dns_nome": "www.nexosix.com.br"})
    assert db.query("SELECT config FROM unidades WHERE id = %s", (uid,), one=True)["config"].get("dns_nome") != "www.nexosix.com.br"


def test_sem_ip_da_lan_nao_devolve(seed, dominio):
    u = _cfg(seed["unidade"]["id"], espelho_token="t")
    dns = failover.DnsNulo()
    failover.ativar(u, dns)
    _cfg(seed["unidade"]["id"], agente_visto_em=datetime.now(timezone.utc).isoformat())  # ping sem ip_lan
    assert failover.verificar(dns, apos_seg=60, volta_seg=0) == []
    assert failover.em_failover(db.query("SELECT * FROM unidades WHERE id = %s", (u["id"],), one=True))


def test_dns_falhando_nao_muda_estado(seed, dominio):
    u = _cfg(seed["unidade"]["id"], espelho_token="t")

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
