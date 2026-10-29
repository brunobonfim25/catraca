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

    # nuvem → agente: o agente reconstrói o cadastro inteiro a partir do outbox
    aplicados, rejeitados = service.aplicar_lote(unidade, itens, lado="agente")
    assert rejeitados == 0 and aplicados == len(itens)
    assert db.query("SELECT count(*) AS n FROM espelho_quarentena", one=True)["n"] == 0
    assert db.query("SELECT count(*) AS n FROM pessoas", one=True)["n"] == 4
    assert pessoas.obter_foto(sincronizado["maria"]["pessoa_id"]) == fotos_antes
    assert db.query("SELECT count(*) AS n FROM eventos", one=True)["n"] == eventos_antes
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],), one=True)["dentro"]
    assert db.query("SELECT config FROM unidades WHERE id = %s", (unidade["id"],), one=True)["config"]["espelho_token"] == "tok-nuvem"
    assert db.query("SELECT count(*) AS n FROM espelho_outbox WHERE id > %s", (outbox_antes,), one=True)["n"] == 0  # aplicar não ecoa
    assert service.aplicar_lote(unidade, itens, lado="agente")[0] == len(itens)  # idempotente
    # hash de senha não viaja: o agente guarda um marcador que nunca confere
    assert {r["senha_hash"] for r in db.query("SELECT senha_hash FROM usuarios_admin")} == {service.SEM_SENHA}


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
    assert "pessoa_fotos" not in tabelas  # fotos vêm paginadas
    assert any(i["tabela"] == "dispositivo_pessoa" for i in s["itens"])
    assert not any(i["tabela"] == "fila_sync" for i in s["itens"])  # tudo concluído
    assert all("senha_hash" not in i["dados"] for i in s["itens"] if i["tabela"] == "usuarios_admin")
    assert int(s["cursor"]["xid"]) > 0 and s["cursor"]["id"] == 0
    pagina = service.fotos(sincronizado["unidade"], apos=0, limite=2)
    assert len(pagina) == 2 and pagina[0]["dados"]["jpeg"].startswith("\\x")
    resto = service.fotos(sincronizado["unidade"], apos=pagina[-1]["chave"]["pessoa_id"], limite=50)
    assert len(pagina) + len(resto) == 3


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


def _h(token, agente="agente-a"):
    return {"X-Espelho-Token": token, "X-Agente-Id": agente}


def test_endpoints_da_nuvem(app, seed):
    nuvem = create_app(NuvemConfig)
    c = nuvem.test_client()
    uid = seed["unidade"]["id"]
    assert c.post("/api/espelho/receber", json={"itens": []}, headers=_h("tok-x")).status_code == 403
    assert app.test_client().post("/api/espelho/receber", json={"itens": []}, headers=_h("tok-x")).status_code == 404  # modo unico não recebe
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "tok-x"}' WHERE id = %s""", (uid,))
    # o agente sobe um evento; turma (cadastro) não é dele e é recusada
    ev = {"id": 10_000_001, "unidade_id": uid, "dispositivo_id": seed["dispositivo"]["id"], "pessoa_id": seed["maria"]["pessoa_id"], "tipo": "ENTRADA",
          "sentido": "entrada", "ocorrido_em": "2026-10-27T10:00:00+00:00", "origem": "dispositivo", "log_id_dispositivo": 99, "detalhes": {}}
    itens = [{"id": 1, "tabela": "eventos", "operacao": "upsert", "chave": {"id": ev["id"]}, "dados": ev},
             {"id": 2, "tabela": "turmas", "operacao": "upsert", "chave": {"id": 77}, "dados": {"id": 77, "unidade_id": uid, "nome": "Pré 3", "turno": "tarde"}}]
    r = c.post("/api/espelho/receber", json={"itens": itens}, headers=_h("tok-x"))
    assert r.status_code == 200 and r.get_json()["aplicados"] == 1 and r.get_json()["rejeitados"] == 1
    assert db.query("SELECT 1 FROM eventos WHERE id = %s", (ev["id"],), one=True)
    assert db.query("SELECT 1 FROM turmas WHERE id = 77", one=True) is None
    assert db.query("SELECT config->>'agente_visto_em' AS v FROM unidades WHERE id = %s", (uid,), one=True)["v"]
    # item quebrado não derruba o lote: vai pra quarentena
    ruim = dict(ev, id=10_000_002, ocorrido_em="não é data", log_id_dispositivo=100)
    r = c.post("/api/espelho/receber", json={"itens": [{"tabela": "eventos", "operacao": "upsert", "chave": {"id": ruim["id"]}, "dados": ruim}]}, headers=_h("tok-x"))
    assert r.status_code == 200
    assert db.query("SELECT tabela FROM espelho_quarentena", one=True)["tabela"] == "eventos"
    # segundo PC com o mesmo token, com o primeiro ativo → recusado
    assert c.post("/api/espelho/ping", json={}, headers=_h("tok-x", "agente-b")).status_code == 409
    # snapshot e reverso
    s = c.get("/api/espelho/snapshot", headers=_h("tok-x")).get_json()
    assert s["ok"] and any(i["tabela"] == "pessoas" for i in s["itens"]) and s["cursor"]["xid"]
    assert c.get("/api/espelho/fotos?apos=0", headers=_h("tok-x")).get_json()["itens"]
    db.execute("DELETE FROM espelho_outbox")
    db.execute("INSERT INTO turmas (id, unidade_id, nome, turno) VALUES (78, %s, 'Pré 4', 'manha')", (uid,))
    db.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES ('turmas','upsert','{\"id\":78}','{}', %s)", (uid,))
    db.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES ('turmas','upsert','{\"id\":79}','{}', 999)")
    rev = c.get("/api/espelho/reverso?desde_xid=0&desde=0", headers=_h("tok-x")).get_json()
    assert [i["chave"]["id"] for i in rev["itens"]] == [78]
    assert rev["itens"][0]["dados"]["nome"] == "Pré 4"  # estado atual da linha, não o que foi gravado no outbox
    c.post("/api/espelho/reverso/confirmar", json={"ate": rev["ate"]}, headers=_h("tok-x"))
    assert c.get("/api/espelho/reverso?desde_xid=0&desde=0", headers=_h("tok-x")).get_json()["itens"] == []
    r = c.post("/api/espelho/ping", json={"versao": "2", "base_url": "http://10.0.0.5", "ip_lan": "8.8.8.8"}, headers=_h("tok-x"))
    assert r.status_code == 200 and r.get_json()["agora"] and r.get_json()["schema_nuvem"] == service.versao_schema()
    agente = db.query("SELECT config->'agente' AS a FROM unidades WHERE id = %s", (uid,), one=True)["a"]
    assert agente["base_url"] == "http://10.0.0.5" and agente["ip_lan"] is None  # IP público não vale
    assert nuvem.test_client().get("/agente").status_code == 404
    # token na URL (rotas antigas) não existe mais
    assert c.post("/api/espelho/tok-x", json={"itens": []}).status_code in (404, 405)


def test_reverso_nao_pula_transacao_lenta(seed):
    """E2: id menor visível depois (transação mais lenta) não pode ser pulado pelo cursor."""
    import psycopg2

    uid = seed["unidade"]["id"]
    unidade = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    db.execute("DELETE FROM espelho_outbox")
    lenta = psycopg2.connect(TestConfig.DATABASE_URL)
    cur = lenta.cursor()
    cur.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES ('turmas','upsert','{\"id\": %s}','{}', %s)", (seed["turma"]["id"], uid))
    # outra transação grava e termina antes
    db.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES ('turmas','upsert','{\"id\": %s}','{}', %s)", (seed["turma_livre"]["id"], uid))
    lote = service.lote_para_agente(unidade)
    assert lote["itens"] == []  # a rápida espera a lenta terminar (xid da lenta < xid da rápida)
    lenta.commit()
    lenta.close()
    lote = service.lote_para_agente(unidade)
    assert sorted(i["chave"]["id"] for i in lote["itens"]) == sorted([seed["turma"]["id"], seed["turma_livre"]["id"]])


def test_outbox_marca_a_escola_pela_linha(outbox_ligado, seed):
    """E9: escrita sem escola na sessão (worker, admin da rede) sai marcada pela própria linha;
    empresa e usuário da rede vão pra todas as escolas da empresa."""
    outra = db.execute("INSERT INTO unidades (empresa_id, nome) VALUES (%s, 'Itaguaí') RETURNING id", (seed["empresa"],), returning=True)["id"]
    db.execute("DELETE FROM espelho_outbox")
    db.contexto.unidade_id = None
    db.execute("UPDATE pessoas SET nome = 'Maria S.' WHERE id = %s", (seed["maria"]["pessoa_id"],))
    db.execute("UPDATE alunos SET matricula = 'X' WHERE id = %s", (seed["maria"]["aluno_id"],))
    db.execute("UPDATE dispositivo_pessoa SET erro = 'x' WHERE pessoa_id = %s", (seed["maria"]["pessoa_id"],))
    db.execute("UPDATE empresas SET nome = 'GEPPP Rede' WHERE id = %s", (seed["empresa"],))
    db.execute("UPDATE usuarios_admin SET nome = 'Admin 2' WHERE id = %s", (seed["admin"]["id"],))
    por = {}
    for r in db.query("SELECT tabela, unidade_id, dados FROM espelho_outbox"):
        por.setdefault(r["tabela"], set()).add(r["unidade_id"])
        if r["tabela"] == "usuarios_admin":
            assert "senha_hash" not in r["dados"]
    uid = seed["unidade"]["id"]
    assert por["pessoas"] == por["alunos"] == por["dispositivo_pessoa"] == {uid}
    assert por["empresas"] == por["usuarios_admin"] == {uid, outra}


def test_agente_nao_escreve_cadastro_nem_reenvia_aviso(seed):
    """S1 / A10: do agente a nuvem só aceita o que é do agente; aviso já enviado não volta pra fila."""
    un = seed["unidade"]
    admin = dict(db.query("SELECT * FROM usuarios_admin WHERE id = %s", (seed["admin"]["id"],), one=True))
    admin = json.loads(json.dumps({**admin, "perfil": "superadmin", "empresa_id": None, "unidade_id": None}, default=str))
    falso = {"id": 999, "nome": "Hacker", "email": "h@x.com", "senha_hash": "x", "perfil": "superadmin", "ativo": True}
    itens = [{"tabela": "usuarios_admin", "operacao": "upsert", "chave": {"id": admin["id"]}, "dados": admin},
             {"tabela": "usuarios_admin", "operacao": "upsert", "chave": {"id": 999}, "dados": falso},
             {"tabela": "pessoas", "operacao": "delete", "chave": {"id": seed["maria"]["pessoa_id"]}},
             {"tabela": "dispositivos", "operacao": "upsert", "chave": {"id": seed["dispositivo"]["id"]},
              "dados": {"id": seed["dispositivo"]["id"], "unidade_id": un["id"], "ip": "6.6.6.6", "senha_api": "x", "status": "online", "config": {"ip_vpn": "6.6.6.6", "logs_no_dispositivo": 3}}}]
    aplicados, rejeitados = service.aplicar_lote(un, itens, lado="nuvem")
    assert rejeitados == 3 and aplicados == 1
    assert db.query("SELECT perfil FROM usuarios_admin WHERE id = %s", (admin["id"],), one=True)["perfil"] == "admin_unidade"
    assert db.query("SELECT 1 FROM usuarios_admin WHERE id = 999", one=True) is None
    assert db.query("SELECT 1 FROM pessoas WHERE id = %s", (seed["maria"]["pessoa_id"],), one=True)
    d = db.query("SELECT * FROM dispositivos WHERE id = %s", (seed["dispositivo"]["id"],), one=True)
    assert d["ip"] == "127.0.0.1" and d["senha_api"] == "admin" and d["config"].get("ip_vpn") is None and d["config"]["logs_no_dispositivo"] == 3
    # aviso: a nuvem já mandou; o agente reenviando a linha 'pendente' não pode voltar pra fila
    nid = db.execute("INSERT INTO fila_notificacao (unidade_id, responsavel_id, texto, status) VALUES (%s,%s,'oi','enviado') RETURNING id",
                     (un["id"], seed["ana"]["responsavel_id"]), returning=True)["id"]
    linha = {"id": nid, "unidade_id": un["id"], "responsavel_id": seed["ana"]["responsavel_id"], "texto": "oi", "status": "pendente", "tentativas": 0}
    service.aplicar_lote(un, [{"tabela": "fila_notificacao", "operacao": "upsert", "chave": {"id": nid}, "dados": linha}], lado="nuvem")
    assert db.query("SELECT status FROM fila_notificacao WHERE id = %s", (nid,), one=True)["status"] == "enviado"


def test_presenca_mais_nova_ganha(seed):
    """E11: presença velha que chega depois não sobrescreve a mais nova."""
    un, pid = seed["unidade"], seed["maria"]["pessoa_id"]
    db.execute("INSERT INTO presenca (pessoa_id, unidade_id, dentro, desde) VALUES (%s,%s,false,'2026-10-27T15:00:00+00')", (pid, un["id"]))
    velha = {"pessoa_id": pid, "unidade_id": un["id"], "dentro": True, "desde": "2026-10-27T10:00:00+00:00"}
    for lado in ("nuvem", "agente"):
        service.aplicar_lote(un, [{"tabela": "presenca", "operacao": "upsert", "chave": {"pessoa_id": pid}, "dados": velha}], lado=lado)
        assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (pid,), one=True)["dentro"] is False


def test_quarentena_reaplica_quando_o_pai_chega(seed):
    un = seed["unidade"]
    filho = {"id": 500, "pessoa_id": 4242, "turma_id": seed["turma"]["id"], "matricula": "Q1"}
    service.aplicar_lote(un, [{"tabela": "alunos", "operacao": "upsert", "chave": {"id": 500}, "dados": filho}], lado="agente")
    assert db.query("SELECT count(*) AS n FROM espelho_quarentena", one=True)["n"] == 1
    pai = {"id": 4242, "unidade_id": un["id"], "tipo": "aluno", "nome": "Chegou Depois", "status": "ativo"}
    service.aplicar_lote(un, [{"tabela": "pessoas", "operacao": "upsert", "chave": {"id": 4242}, "dados": pai}], lado="agente")
    assert db.query("SELECT matricula FROM alunos WHERE id = 500", one=True)["matricula"] == "Q1"
    assert db.query("SELECT count(*) AS n FROM espelho_quarentena", one=True)["n"] == 0


def test_outbox_velho_vira_recarga_e_nao_some_sem_aviso(seed):
    """E7: linha não buscada pelo agente não é apagada em silêncio."""
    uid = seed["unidade"]["id"]
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "t"}' WHERE id = %s""", (uid,))
    db.execute("DELETE FROM espelho_outbox")
    db.execute("INSERT INTO espelho_outbox (tabela, operacao, chave, unidade_id, criado_em) VALUES ('turmas','upsert','{\"id\":1}', %s, now() - interval '10 days')", (uid,))
    service.limpar_outbox_da_nuvem()
    assert db.query("SELECT count(*) AS n FROM espelho_outbox", one=True)["n"] == 1  # 10 dias: fica
    db.execute("UPDATE espelho_outbox SET criado_em = now() - interval '40 days'")
    service.limpar_outbox_da_nuvem()
    unidade = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    assert unidade["config"]["espelho_recarga"] is True
    with pytest.raises(service.RecargaNecessaria):
        service.lote_para_agente(unidade)
    service.snapshot(unidade)  # a carga nova limpa a marca
    assert not db.query("SELECT config FROM unidades WHERE id = %s", (uid,), one=True)["config"]["espelho_recarga"]


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


def test_instalador_exe_e_codigo_de_instalacao(app, seed):
    """O .exe é o mesmo para todas as escolas; o código de uso único diz de qual escola ele é."""
    nuvem = create_app(NuvemConfig)
    nuvem.config["BASE_URL"] = "https://nuvem.exemplo"
    uid = seed["unidade"]["id"]
    anonimo = nuvem.test_client()
    assert anonimo.get(f"/unidades/{uid}/instalador.exe").status_code in (302, 401)
    assert anonimo.get(f"/unidades/{uid}/instalador.cmd").status_code == 404  # o .cmd com token dentro não existe mais
    assert anonimo.get("/api/espelho/pacote.zip").status_code == 404
    rec = nuvem.test_client()
    rec.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"})
    assert rec.get(f"/unidades/{uid}/instalador.exe").status_code == 404
    assert rec.post(f"/unidades/{uid}/codigo-instalacao").status_code == 403
    c = nuvem.test_client()
    c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"})
    r = c.get(f"/unidades/{uid}/instalador.exe")
    assert r.status_code == 302 and r.headers["Location"].endswith("/NexosixAgente-Setup.exe")
    # o código aparece uma vez, na tela da escola
    r = c.post(f"/unidades/{uid}/codigo-instalacao", follow_redirects=True)
    html = r.get_data(as_text=True)
    import re

    codigo = re.search(r"font-monospace\">([A-Z0-9]{4}-[A-Z0-9]{4})<", html).group(1)
    assert codigo not in c.get(f"/unidades/{uid}/config").get_data(as_text=True)
    assert db.query("SELECT config->'instalacao'->>'hash' AS h FROM unidades WHERE id = %s", (uid,), one=True)["h"] != codigo
    # conferir não consome; minúsculas e sem hífen também valem (é digitado à mão)
    v = anonimo.post("/api/espelho/ativar", json={"codigo": codigo.lower().replace("-", ""), "so_verificar": True})
    assert v.status_code == 200 and v.get_json()["unidade_nome"] == "Recreio" and "token" not in v.get_json()
    a = anonimo.post("/api/espelho/ativar", json={"codigo": codigo, "maquina": "PC-RECEPCAO"}).get_json()
    assert a["ok"] and a["unidade_id"] == uid and a["nuvem_url"] == "https://nuvem.exemplo"
    un = db.query("SELECT config FROM unidades WHERE id = %s", (uid,), one=True)["config"]
    assert un["espelho_token"] == a["token"] and "instalacao" not in un
    # uso único: a segunda vez não serve
    assert anonimo.post("/api/espelho/ativar", json={"codigo": codigo}).status_code == 404
    # o token novo funciona; ativar de novo (outro PC) troca o token e o antigo para
    assert anonimo.get("/api/espelho/snapshot", headers={"X-Espelho-Token": a["token"]}).status_code == 200
    from app.espelho import instalacao

    codigo2, _ = instalacao.gerar(uid)
    b = anonimo.post("/api/espelho/ativar", json={"codigo": codigo2}).get_json()
    assert b["token"] != a["token"]
    assert anonimo.get("/api/espelho/snapshot", headers={"X-Espelho-Token": a["token"]}).status_code == 403
    assert db.query("SELECT 1 FROM auditoria WHERE acao = 'ativar_servidor_escola'", one=True)
    assert db.query("SELECT 1 FROM auditoria WHERE acao = 'baixar_instalador'", one=True)
    # código vencido não vale
    codigo3, _ = instalacao.gerar(uid)
    db.execute("""UPDATE unidades SET config = jsonb_set(config, '{instalacao,expira}', '"2020-01-01T00:00:00+00:00"') WHERE id = %s""", (uid,))
    assert anonimo.post("/api/espelho/ativar", json={"codigo": codigo3, "so_verificar": True}).status_code == 404
    db.gucs, db.contexto.unidade_id = {}, None


def test_ativar_bloqueia_quem_chuta_codigo(app, seed):
    nuvem = create_app(NuvemConfig)
    c = nuvem.test_client()
    for _ in range(5):
        assert c.post("/api/espelho/ativar", json={"codigo": "AAAA-AAAA"}).status_code == 404
    from app.espelho import instalacao

    codigo, _ = instalacao.gerar(seed["unidade"]["id"])
    r = c.post("/api/espelho/ativar", json={"codigo": codigo})
    assert r.status_code == 429  # nem o certo passa enquanto o IP está bloqueado
    # no agente (MODO=local) a rota não existe
    assert app.test_client().post("/api/espelho/ativar", json={"codigo": codigo}).status_code == 404
    db.gucs, db.contexto.unidade_id = {}, None


def test_fusos_funcionam_sem_base_do_sistema():
    """Windows não tem /usr/share/zoneinfo: o pacote tzdata precisa estar instalado."""
    import importlib.util

    assert importlib.util.find_spec("tzdata"), "tzdata fora do requirements: o agente quebra no Windows"
    from zoneinfo import ZoneInfo

    import tzdata  # noqa: F401
    assert ZoneInfo("America/Sao_Paulo").key == "America/Sao_Paulo"
