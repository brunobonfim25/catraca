"""Espelho local → nuvem: outbox por trigger, envio, aplicação idempotente, somente leitura."""
import json

import pytest

from app import create_app, db
from app.config import TestConfig
from app.espelho import service
from app.pessoas import service as pessoas
from tests.conftest import JPEG_OK, access_log, dao


def _tabelas_dominio():
    return [t for t in service.TABELAS]


def test_outbox_registra_insert_update_delete(seed):
    tabelas = {r["tabela"] for r in db.query("SELECT DISTINCT tabela FROM espelho_outbox")}
    assert {"empresas", "unidades", "turmas", "pessoas", "pessoa_fotos", "alunos", "responsaveis", "aluno_responsavel", "dispositivos", "auditoria"} <= tabelas
    assert "fila_sync" not in tabelas
    db.execute("DELETE FROM espelho_outbox")
    pessoas.desvincular(seed["maria"]["aluno_id"], seed["ana"]["responsavel_id"])
    item = db.query("SELECT * FROM espelho_outbox WHERE tabela = 'aluno_responsavel'", one=True)
    assert item["operacao"] == "delete" and item["chave"] == {"aluno_id": seed["maria"]["aluno_id"], "responsavel_id": seed["ana"]["responsavel_id"]}
    foto = db.query("SELECT dados FROM espelho_outbox WHERE tabela = 'pessoa_fotos'", one=True)
    assert foto is None  # foto não mudou nesse passo


def test_aplicar_lote_reconstroi_a_unidade_na_nuvem(cliente, sincronizado):
    """Pega tudo que o local gerou, zera o banco (como se fosse a nuvem vazia) e aplica."""
    dao(cliente, access_log(1, sincronizado["user_ids"]["maria"], 1))
    itens = service.lote_pendente(limite=100000)
    assert len(itens) > 20
    fotos_antes = pessoas.obter_foto(sincronizado["maria"]["pessoa_id"])
    eventos_antes = db.query("SELECT count(*) AS n FROM eventos", one=True)["n"]
    unidade = dict(sincronizado["unidade"])
    unidade["config"] = {**unidade["config"], "espelho_token": "tok-nuvem"}
    tabelas = ", ".join(t for t in _tabelas_dominio() if t not in ("empresas", "unidades"))
    db.execute(f"TRUNCATE {tabelas} RESTART IDENTITY CASCADE")
    db.execute("UPDATE unidades SET config = %s WHERE id = %s", (json.dumps(unidade["config"]), unidade["id"]))
    assert db.query("SELECT count(*) AS n FROM pessoas", one=True)["n"] == 0
    outbox_antes = db.query("SELECT max(id) AS m FROM espelho_outbox", one=True)["m"]

    aplicados, rejeitados = service.aplicar_lote(unidade, itens)
    assert rejeitados == 0 and aplicados == len(itens)
    assert db.query("SELECT count(*) AS n FROM pessoas", one=True)["n"] == 4
    assert pessoas.obter_foto(sincronizado["maria"]["pessoa_id"]) == fotos_antes  # bytea voltou igual
    assert db.query("SELECT count(*) AS n FROM eventos", one=True)["n"] == eventos_antes
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],), one=True)["dentro"]
    assert db.query("SELECT regra, dias_semana FROM aluno_responsavel", one=True)["dias_semana"] == []
    cfg = db.query("SELECT config FROM unidades WHERE id = %s", (unidade["id"],), one=True)["config"]
    assert cfg["espelho_token"] == "tok-nuvem" and cfg["espelho_ultimo_em"]  # token da nuvem sobreviveu ao upsert
    # a nuvem não gera outbox ao aplicar
    assert db.query("SELECT count(*) AS n FROM espelho_outbox WHERE id > %s", (outbox_antes,), one=True)["n"] == 0
    # reaplicar é idempotente
    assert service.aplicar_lote(unidade, itens)[0] == len(itens)
    assert db.query("SELECT count(*) AS n FROM pessoas", one=True)["n"] == 4


def test_rejeita_linhas_de_outra_unidade(seed):
    outra = dict(seed["unidade"]); outra["id"] = 999
    itens = [{"id": 1, "tabela": "turmas", "operacao": "upsert", "chave": {"id": 5}, "dados": {"id": 5, "unidade_id": 1, "nome": "x", "turno": "manha"}},
             {"id": 2, "tabela": "fila_sync", "operacao": "upsert", "chave": {"id": 1}, "dados": {}}]
    assert service.aplicar_lote(outra, itens) == (0, 2)


def test_enviar_pendentes_confirma_so_com_ok(seed):
    chamadas = []

    class Resp:
        def __init__(self, ok, corpo):
            self.ok, self._c, self.status_code, self.text = ok, corpo, (200 if ok else 500), ""

        def json(self):
            return self._c

    def post_ok(url, data, headers, timeout):
        chamadas.append(json.loads(data))
        return Resp(True, {"ok": True})

    antes = db.query("SELECT count(*) AS n FROM espelho_outbox", one=True)["n"]
    assert antes > 0
    n = service.enviar_pendentes("https://nuvem", "tok", limite=10, post=post_ok)
    assert n == antes and chamadas[0]["itens"][0]["tabela"] and db.query("SELECT count(*) AS n FROM espelho_outbox", one=True)["n"] == 0
    pessoas.criar_visitante(seed["unidade"]["id"], "Zé")
    with pytest.raises(service.EspelhoErro):
        service.enviar_pendentes("https://nuvem", "tok", post=lambda *a, **k: Resp(False, {"ok": False, "erro": "x"}))
    assert db.query("SELECT count(*) AS n FROM espelho_outbox", one=True)["n"] > 0
    assert service.enviar_pendentes("", "", post=post_ok) == 0


class NuvemConfig(TestConfig):
    MODO = "nuvem"


def test_endpoint_da_nuvem_e_somente_leitura(app, seed):
    nuvem = create_app(NuvemConfig)
    db.gucs = {"nexosix.outbox": "on"}  # create_app da nuvem zera; os outros testes continuam locais
    c = nuvem.test_client()
    assert c.post("/api/espelho/tok-x", json={"itens": []}).status_code == 403
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "tok-x"}' WHERE id = %s""", (seed["unidade"]["id"],))
    itens = [{"id": 1, "tabela": "turmas", "operacao": "upsert", "chave": {"id": 77}, "dados": {"id": 77, "unidade_id": seed["unidade"]["id"], "nome": "Pré 3", "turno": "tarde"}}]
    r = c.post("/api/espelho/tok-x", json={"itens": itens})
    assert r.status_code == 200 and r.get_json()["aplicados"] == 1
    assert db.query("SELECT nome FROM turmas WHERE id = 77", one=True)["nome"] == "Pré 3"
    assert nuvem.test_client().post("/api/espelho/tok-x", json={"itens": [{"tabela": "turmas", "operacao": "upsert", "chave": {}, "dados": {"id": "abc", "unidade_id": seed["unidade"]["id"]}}]}).status_code == 500
    # painel da nuvem: lê, não escreve
    c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"})
    assert c.get("/painel").status_code == 200
    r = c.post("/turmas/nova", data={"nome": "Nova", "turno": "manha"})
    assert r.status_code == 302 and db.query("SELECT 1 FROM turmas WHERE nome = 'Nova'", one=True) is None
    assert c.get("/painel/rede").status_code == 200
    assert app.test_client().post("/api/espelho/tok-x", json={"itens": []}).status_code == 404  # servidor local não recebe espelho


def test_configurar_local_reserva_faixa_de_ids(app, seed):
    runner = app.test_cli_runner()
    r = runner.invoke(args=["configurar-local", "--empresa-id", "1", "--unidade-id", "3", "--empresa", "GEPPP", "--unidade", "Barra"])
    assert r.exit_code == 0, r.output
    assert db.query("SELECT nome FROM unidades WHERE id = 3", one=True)["nome"] == "Barra"
    pid = pessoas.criar_visitante(3, "Visita")
    assert pid > 30_000_000
    r = runner.invoke(args=["token-espelho", "--unidade-id", "3"])
    assert "ESPELHO_TOKEN=" in r.output
    assert db.query("SELECT config->>'espelho_token' AS t FROM unidades WHERE id = 3", one=True)["t"]
