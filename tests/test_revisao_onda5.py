"""Revisão de 27/10/2026, onda 5: segurança restante e operação."""
import io
import zipfile

import pytest

from app import alertas, create_app, db
from app.config import TestConfig


class ComCsrf(TestConfig):
    CSRF_ATIVO = True


def test_S13_post_sem_token_csrf_e_recusado(seed):
    app = create_app(ComCsrf)
    c = app.test_client()
    # login também exige o token (sem ele, um site de fora loga a recepção na conta dele)
    assert c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"}).status_code == 400
    html = c.get("/login").get_data(as_text=True)
    token = html.split('const t = "', 1)[1].split('"', 1)[0]
    assert c.post("/login", data={"email": "admin@teste.com", "senha": "senha123", "_csrf": token}).status_code == 302
    # liberar a catraca vindo de outro site: sem token, recusado
    r = c.post("/painel/liberar", data={"pessoa_id": seed["maria"]["pessoa_id"], "sentido": "saida", "justificativa": "x"})
    assert r.status_code == 400
    assert not db.query("SELECT 1 FROM eventos WHERE origem = 'manual'", one=True)
    # catraca, agente e Telegram não usam formulário: continuam sem token
    assert c.post("/api/catraca/tok-teste/session_is_valid.fcgi").status_code == 200
    db.gucs, db.contexto.unidade_id = {}, None


def test_S14_bloqueio_progressivo_e_sem_dica_de_email(app, seed):
    c = app.test_client()
    for _ in range(5):
        assert c.post("/login", data={"email": "admin@teste.com", "senha": "errada"}).status_code == 200
    r = c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"})  # nem a certa entra
    assert r.status_code == 429 and "Muitas tentativas" in r.get_data(as_text=True)
    assert db.query("SELECT count(*) AS n FROM auditoria WHERE acao = 'login_falhou'", one=True)["n"] == 5
    db.execute("UPDATE login_falhas SET bloqueado_ate = now() - interval '1 second'")
    assert c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"}).status_code == 302
    assert db.query("SELECT 1 FROM auditoria WHERE acao = 'login'", one=True)
    # e-mail inexistente responde igual ao existente
    r = app.test_client().post("/login", data={"email": "nao-existe@teste.com", "senha": "x"})
    assert r.status_code == 200 and "inválidos" in r.get_data(as_text=True)


def test_S18_nuvem_nao_sobe_com_chave_padrao():
    class NuvemFraca(TestConfig):
        MODO, TESTING, SECRET_KEY = "nuvem", False, "dev-insegura"

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(NuvemFraca)
    db.gucs, db.contexto.unidade_id = {}, None


def test_S19_trocar_senha_derruba_as_outras_sessoes(app, seed):
    a, b = app.test_client(), app.test_client()
    for c in (a, b):
        c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"})
    assert b.get("/painel").status_code == 200
    a.post("/senha", data={"atual": "senha123", "nova": "outra-senha-123", "confirma": "outra-senha-123"})
    assert a.get("/painel").status_code == 200       # quem trocou continua
    assert b.get("/painel").status_code == 302       # a outra sessão cai


def test_S19_cabecalhos_e_auditoria_da_foto(cliente, seed):
    r = cliente.get("/painel")
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
    pid = seed["maria"]["pessoa_id"]
    cliente.get(f"/pessoas/{pid}/foto.jpg")
    cliente.get(f"/pessoas/{pid}/foto.jpg")  # na mesma hora: uma linha só
    cliente.get(f"/pessoas/{pid}/foto.jpg?mini=1")
    assert db.query("SELECT count(*) AS n FROM auditoria WHERE acao = 'ver_foto'", one=True)["n"] == 1


def test_O2_alertas_comecam_e_terminam(seed, monkeypatch):
    uid = seed["unidade"]["id"]
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "t", "agente_visto_em": "2026-01-01T10:00:00+00:00"}' WHERE id = %s""", (uid,))
    enviados = []
    monkeypatch.setenv("ALERTA_TELEGRAM_CHAT", "42")
    msgs = alertas.verificar(enviar=lambda chat, texto, **k: enviados.append((chat, texto)))
    assert any("sem sinal" in m for m in msgs) and enviados[0][0] == "42"
    assert alertas.verificar(enviar=lambda *a, **k: enviados.append(a)) == []  # não repete
    from datetime import datetime, timezone

    db.execute("UPDATE unidades SET config = config || jsonb_build_object('agente_visto_em', %s::text) WHERE id = %s",
               (datetime.now(timezone.utc).isoformat(), uid))
    assert any(m.startswith("✅ Resolvido") for m in alertas.verificar(enviar=lambda *a, **k: None))


def test_O2_saude_do_worker(app):
    c = app.test_client()
    assert c.get("/health/worker").status_code == 503
    alertas.batimento("unico")
    assert c.get("/health/worker").get_json()["ok"] is True


def test_O6_migrar_de_novo_nao_faz_nada_e_espera_a_trava():
    import threading

    assert db.migrar(log=lambda m: None) == 0
    resultados = []
    ts = [threading.Thread(target=lambda: resultados.append(db.migrar(log=lambda m: None))) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert resultados == [0, 0, 0]


def test_O11_exportar_dados_da_escola(cliente, seed):
    r = cliente.get(f"/unidades/{seed['unidade']['id']}/exportar.zip")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert {"alunos.csv", "responsaveis.csv", "vinculos.csv", "consentimentos.csv", "eventos_12_meses.csv"} <= set(z.namelist())
    alunos = z.read("alunos.csv").decode("utf-8-sig")
    assert "Maria Silva" in alunos and ";" in alunos
    assert not any(n.endswith(".jpg") for n in z.namelist())
    assert db.query("SELECT 1 FROM auditoria WHERE acao = 'exportar_dados'", one=True)
    rec = cliente.application.test_client()
    rec.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"})
    assert rec.get(f"/unidades/{seed['unidade']['id']}/exportar.zip").status_code == 403
