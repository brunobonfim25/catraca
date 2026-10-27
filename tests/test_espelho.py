"""Outbox por trigger, aplicação idempotente de lotes, snapshot, endpoints da nuvem."""
import json

import pytest

from app import create_app, db
from app.config import TestConfig
from app.espelho import service
from app.pessoas import service as pessoas
from tests.conftest import access_log, dao


def test_outbox_registra_insert_update_delete_e_unidade(outbox_ligado, seed):
    tabelas = {r["tabela"] for r in db.query("SELECT DISTINCT tabela FROM espelho_outbox")}
    assert {"empresas", "unidades", "turmas", "pessoas", "pessoa_fotos", "alunos", "responsaveis", "aluno_responsavel", "dispositivos", "fila_sync", "auditoria"} <= tabelas
    db.execute("DELETE FROM espelho_outbox")
    db.contexto.unidade_id = seed["unidade"]["id"]
    pessoas.desvincular(seed["maria"]["aluno_id"], seed["ana"]["responsavel_id"])
    item = db.query("SELECT * FROM espelho_outbox WHERE tabela = 'aluno_responsavel'", one=True)
    assert item["operacao"] == "delete" and item["unidade_id"] == seed["unidade"]["id"]
    assert item["chave"] == {"aluno_id": seed["maria"]["aluno_id"], "responsavel_id": seed["ana"]["responsavel_id"]}
    # escrita com outbox=False (controle) não gera linha
    db.execute("UPDATE unidades SET config = config || '{\"x\": 1}' WHERE id = %s", (seed["unidade"]["id"],), outbox=False)
    assert db.query("SELECT count(*) AS n FROM espelho_outbox WHERE tabela = 'unidades'", one=True)["n"] == 0


def test_aplicar_lote_reconstroi_a_unidade(outbox_ligado, cliente, sincronizado):
    dao(cliente, access_log(1, sincronizado["user_ids"]["maria"], 1))
    itens = service.lote_pendente(limite=100000)
    fotos_antes = pessoas.obter_foto(sincronizado["maria"]["pessoa_id"])
    eventos_antes = db.query("SELECT count(*) AS n FROM eventos", one=True)["n"]
    unidade = dict(sincronizado["unidade"])
    unidade["config"] = {**unidade["config"], "espelho_token": "tok-nuvem"}
    db.execute("TRUNCATE " + ", ".join(t for t in service.TABELAS if t not in ("empresas", "unidades")) + " RESTART IDENTITY CASCADE", outbox=False)
    db.execute("UPDATE unidades SET config = %s WHERE id = %s", (json.dumps(unidade["config"]), unidade["id"]), outbox=False)
    outbox_antes = db.query("SELECT max(id) AS m FROM espelho_outbox", one=True)["m"]

    aplicados, rejeitados = service.aplicar_lote(unidade, itens)
    assert rejeitados == 0 and aplicados == len(itens)
    assert db.query("SELECT count(*) AS n FROM pessoas", one=True)["n"] == 4
    assert pessoas.obter_foto(sincronizado["maria"]["pessoa_id"]) == fotos_antes
    assert db.query("SELECT count(*) AS n FROM eventos", one=True)["n"] == eventos_antes
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],), one=True)["dentro"]
    assert db.query("SELECT config FROM unidades WHERE id = %s", (unidade["id"],), one=True)["config"]["espelho_token"] == "tok-nuvem"
    assert db.query("SELECT count(*) AS n FROM espelho_outbox WHERE id > %s", (outbox_antes,), one=True)["n"] == 0  # aplicar não ecoa
    assert service.aplicar_lote(unidade, itens)[0] == len(itens)  # idempotente


def test_agente_protege_status_da_catraca(sincronizado):
    d = sincronizado["dispositivo"]
    db.execute("UPDATE dispositivos SET status = 'online', ultimo_log_id = 50, firmware = 'fw' WHERE id = %s", (d["id"],))
    linha = dict(db.query("SELECT * FROM dispositivos WHERE id = %s", (d["id"],), one=True))
    linha.update({"nome": "Catraca renomeada", "status": "desconhecido", "ultimo_log_id": 0, "firmware": None, "ip": "10.0.0.9"})
    linha = json.loads(json.dumps(linha, default=str))
    item = {"id": 1, "tabela": "dispositivos", "operacao": "upsert", "chave": {"id": d["id"]}, "dados": linha}
    service.aplicar_lote(sincronizado["unidade"], [item], lado="agente")
    row = db.query("SELECT * FROM dispositivos WHERE id = %s", (d["id"],), one=True)
    assert row["nome"] == "Catraca renomeada" and row["ip"] == "10.0.0.9"
    assert row["status"] == "online" and row["ultimo_log_id"] == 50 and row["firmware"] == "fw"  # a nuvem não sabe disso
    service.aplicar_lote(sincronizado["unidade"], [item], lado="nuvem")
    assert db.query("SELECT ultimo_log_id FROM dispositivos WHERE id = %s", (d["id"],), one=True)["ultimo_log_id"] == 0


def test_rejeita_linhas_de_outra_unidade(seed):
    outra = dict(seed["unidade"]); outra["id"] = 999
    itens = [{"id": 1, "tabela": "turmas", "operacao": "upsert", "chave": {"id": 5}, "dados": {"id": 5, "unidade_id": 1, "nome": "x", "turno": "manha"}},
             {"id": 2, "tabela": "nao_existe", "operacao": "upsert", "chave": {"id": 1}, "dados": {}}]
    assert service.aplicar_lote(outra, itens) == (0, 2)


def test_snapshot_traz_o_cadastro_da_unidade(sincronizado):
    s = service.snapshot(sincronizado["unidade"])
    tabelas = [i["tabela"] for i in s["itens"]]
    assert tabelas.index("unidades") < tabelas.index("pessoas") < tabelas.index("alunos")
    foto = next(i for i in s["itens"] if i["tabela"] == "pessoa_fotos" and i["chave"]["pessoa_id"] == sincronizado["maria"]["pessoa_id"])
    assert foto["dados"]["jpeg"].startswith("\\x")
    assert any(i["tabela"] == "dispositivo_pessoa" for i in s["itens"])
    assert not any(i["tabela"] == "fila_sync" for i in s["itens"])  # tudo concluído


def test_empurrar_confirma_so_com_ok(outbox_ligado, seed):
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
    assert service.empurrar("https://nuvem", "tok", post=post_ok, limite=10) == antes
    assert db.query("SELECT count(*) AS n FROM espelho_outbox", one=True)["n"] == 0
    pessoas.criar_visitante(seed["unidade"]["id"], "Zé")
    with pytest.raises(service.EspelhoErro):
        service.empurrar("https://nuvem", "tok", post=lambda *a, **k: Resp(False, {"ok": False, "erro": "x"}))
    assert db.query("SELECT count(*) AS n FROM espelho_outbox", one=True)["n"] > 0


class NuvemConfig(TestConfig):
    MODO = "nuvem"


def test_endpoints_da_nuvem(app, seed):
    nuvem = create_app(NuvemConfig)
    c = nuvem.test_client()
    assert c.post("/api/espelho/tok-x", json={"itens": []}).status_code == 403
    assert app.test_client().post("/api/espelho/tok-x", json={"itens": []}).status_code == 404  # modo unico não recebe
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "tok-x"}' WHERE id = %s""", (seed["unidade"]["id"],))
    itens = [{"id": 1, "tabela": "turmas", "operacao": "upsert", "chave": {"id": 77}, "dados": {"id": 77, "unidade_id": seed["unidade"]["id"], "nome": "Pré 3", "turno": "tarde"}}]
    r = c.post("/api/espelho/tok-x", json={"itens": itens})
    assert r.status_code == 200 and r.get_json()["aplicados"] == 1
    assert db.query("SELECT nome FROM turmas WHERE id = 77", one=True)["nome"] == "Pré 3"
    assert db.query("SELECT config->>'agente_visto_em' AS v FROM unidades WHERE id = %s", (seed["unidade"]["id"],), one=True)["v"]
    assert c.post("/api/espelho/tok-x", json={"itens": [{"tabela": "turmas", "operacao": "upsert", "chave": {}, "dados": {"id": "abc", "unidade_id": seed["unidade"]["id"]}}]}).status_code == 500
    # snapshot e reverso
    s = c.get("/api/espelho/tok-x/snapshot").get_json()
    assert s["ok"] and any(i["tabela"] == "pessoas" for i in s["itens"])
    db.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES ('turmas','upsert','{\"id\":78}','{\"id\":78,\"unidade_id\":1,\"nome\":\"x\",\"turno\":\"manha\"}', %s)", (seed["unidade"]["id"],))
    db.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES ('turmas','upsert','{\"id\":79}','{}', 999)")
    rev = c.get("/api/espelho/tok-x/reverso?desde=0").get_json()
    assert [i["chave"]["id"] for i in rev["itens"]] == [78]
    c.post("/api/espelho/tok-x/reverso/confirmar", json={"ate": rev["itens"][0]["id"]})
    assert c.get("/api/espelho/tok-x/reverso?desde=0").get_json()["itens"] == []
    assert c.post("/api/espelho/tok-x/ping", json={"versao": "1", "base_url": "http://10.0.0.5:8000"}).status_code == 200
    assert db.query("SELECT config->'agente'->>'base_url' AS b FROM unidades WHERE id = %s", (seed["unidade"]["id"],), one=True)["b"] == "http://10.0.0.5:8000"
    assert nuvem.test_client().get("/agente").status_code == 404


class AgenteConfig(TestConfig):
    MODO = "local"
    NUVEM_URL = "https://nuvem.exemplo"
    UNIDADE_ID = 1


def test_agente_so_serve_api_e_status(app, seed):
    agente = create_app(AgenteConfig)
    c = agente.test_client()
    r = c.get("/painel")
    assert r.status_code == 302 and r.headers["Location"] == "https://nuvem.exemplo/painel"
    assert c.get("/agente").status_code == 200
    assert c.get("/").status_code == 302 and c.get("/health").get_json()["modo"] == "local"
    assert c.post("/api/catraca/tok-teste/session_is_valid.fcgi").status_code == 200
    db.gucs, db.contexto.unidade_id = {}, None


def test_configurar_local_reserva_faixa_de_ids(app, seed):
    runner = app.test_cli_runner()
    r = runner.invoke(args=["configurar-local", "--unidade-id", "3"])
    assert r.exit_code == 0, r.output
    pid = pessoas.criar_visitante(seed["unidade"]["id"], "Visita")
    assert pid > 30_000_000
    r = runner.invoke(args=["token-espelho", "--unidade-id", str(seed["unidade"]["id"])])
    assert "ESPELHO_TOKEN=" in r.output


def test_instalador_personalizado_so_com_token(app, seed):
    nuvem = create_app(NuvemConfig)
    nuvem.config["BASE_URL"] = "https://nuvem.exemplo"
    c = nuvem.test_client()
    assert c.get("/instalador/nada.ps1").status_code == 403
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "tok-inst"}' WHERE id = %s""", (seed["unidade"]["id"],))
    ps1 = c.get("/instalador/tok-inst.ps1")
    assert ps1.status_code == 200
    corpo = ps1.get_data(as_text=True)
    assert '$TOKEN       = "tok-inst"' in corpo and '$NUVEM_URL   = "https://nuvem.exemplo"' in corpo and f'$UNIDADE_ID  = "{seed["unidade"]["id"]}"' in corpo
    assert not any(ph in corpo for ph in ("__NUVEM_URL__", "__TOKEN__", "__UNIDADE_ID__", "__UNIDADE_NOME__"))  # nenhum placeholder sobrou
    cmd = c.get("/instalador/tok-inst.cmd")
    assert cmd.status_code == 200 and "attachment" in cmd.headers["Content-Disposition"]
    assert "https://nuvem.exemplo/instalador/tok-inst.ps1" in cmd.get_data(as_text=True)
    assert app.test_client().get("/instalador/tok-inst.ps1").status_code == 404  # só a nuvem serve
    db.gucs, db.contexto.unidade_id = {}, None
