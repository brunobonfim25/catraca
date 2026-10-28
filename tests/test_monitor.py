"""Monitoramento em tempo real: incidentes, botão "Assumir", webhook do Railway, tela."""
from psycopg2.extras import Json

from app import alertas, db
from app.notificacoes import telegram


def _mensagens():
    enviados = []

    def enviar(chat, texto, reply_markup=None):
        enviados.append((chat, texto, reply_markup))

    return enviados, enviar


def _agente_mudo(uid, minutos=10):
    from datetime import datetime, timedelta, timezone

    visto = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"espelho_token": "t", "agente_visto_em": visto}), uid))


def test_incidente_repete_ate_alguem_assumir_e_fecha_com_duracao(seed, monkeypatch):
    monkeypatch.setenv("ALERTA_TELEGRAM_CHAT", "-100")
    uid = seed["unidade"]["id"]
    _agente_mudo(uid)
    enviados, enviar = _mensagens()
    alertas.verificar(enviar)
    chat, texto, botao = enviados[0]
    assert chat == "-100" and texto.startswith("🚨") and "sem sinal" in texto
    inc_id = botao["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    alertas.verificar(enviar)
    assert len(enviados) == 1  # ainda não passaram 15 min
    db.execute("UPDATE incidentes SET ultimo_aviso = now() - interval '16 minutes'")
    alertas.verificar(enviar)
    assert enviados[-1][1].startswith("⏰ Sem resposta")
    # alguém assume pelo botão no grupo: para de insistir
    r = telegram.processar_update({"callback_query": {"id": "c", "from": {"id": 7, "first_name": "Bruno"}, "data": f"inc:{inc_id}",
                                                      "message": {"message_id": 9, "chat": {"id": -100}}}})
    assert r["aviso"] == "Assumido por Bruno."
    db.execute("UPDATE incidentes SET ultimo_aviso = now() - interval '16 minutes'")
    n = len(enviados)
    alertas.verificar(enviar)
    assert len(enviados) == n
    # o agente volta: fecha e diz quanto durou
    _agente_mudo(uid, minutos=0)
    alertas.verificar(enviar)
    assert enviados[-1][1].startswith("✅ Resolvido em")
    inc = db.query("SELECT * FROM incidentes WHERE id = %s", (int(inc_id),), one=True)
    assert inc["fim"] and inc["reconhecido_por"] == "Bruno" and inc["avisos"] == 2


def test_botao_assumir_so_vale_no_chat_de_alertas(seed, monkeypatch):
    monkeypatch.setenv("ALERTA_TELEGRAM_CHAT", "-100")
    inc_id = alertas.abrir("teste", "Teste")
    r = telegram.processar_update({"callback_query": {"id": "c", "from": {"id": 5, "first_name": "Estranho"}, "data": f"inc:{inc_id}",
                                                      "message": {"message_id": 1, "chat": {"id": 5}}}})
    assert r["aviso"] == "Este botão não vale aqui."
    assert db.query("SELECT reconhecido_por FROM incidentes WHERE id = %s", (inc_id,), one=True)["reconhecido_por"] is None


def test_detecta_catraca_fora_e_avisos_parados(seed):
    db.execute("UPDATE dispositivos SET status = 'contingencia', ultimo_contato = now() - interval '5 minutes'")
    rid = seed["ana"]["responsavel_id"]
    db.execute("INSERT INTO fila_notificacao (unidade_id, responsavel_id, texto, criado_em) VALUES (%s,%s,'x', now() - interval '10 minutes')",
               (seed["unidade"]["id"], rid))
    chaves = {p["chave"]: p for p in alertas.verificacoes()}
    assert "contingência" in chaves[f"catraca:{seed['dispositivo']['id']}"]["texto"]
    assert "avisos_atrasados" in chaves
    db.execute("UPDATE dispositivos SET ultimo_contato = now()")  # 5 min é o limite? não: 3 min
    assert f"catraca:{seed['dispositivo']['id']}" not in {p["chave"] for p in alertas.verificacoes()}


def test_webhook_do_railway(app, monkeypatch):
    monkeypatch.setenv("RAILWAY_WEBHOOK_TOKEN", "seg")
    c = app.test_client()
    corpo = {"type": "Deployment.crashed", "resource": {"service": {"name": "worker"}}}
    assert c.post("/api/railway/webhook?token=errado", json=corpo).status_code == 403
    assert c.post("/api/railway/webhook?token=seg", json=corpo).get_json()["resultado"] == "aberto"
    assert db.query("SELECT texto FROM incidentes WHERE chave = 'deploy:worker' AND fim IS NULL", one=True)["texto"] == "Railway: worker caiu"
    assert "deploy:worker" in {p["chave"] for p in alertas.verificacoes()}  # fica aberto até o Railway dizer que voltou
    corpo["type"] = "Deployment.deployed"
    assert c.post("/api/railway/webhook?token=seg", json=corpo).get_json()["resultado"] == "fechado"


def test_tela_do_monitor_so_para_a_nexosix(cliente, seed, app):
    assert cliente.get("/painel/monitor").status_code == 403  # admin da escola
    db.execute("UPDATE usuarios_admin SET perfil = 'superadmin', empresa_id = NULL, unidade_id = NULL WHERE email = 'admin@teste.com'")
    c = app.test_client()
    c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"})
    assert c.get("/painel/monitor").status_code == 200
    d = c.get("/painel/monitor/estado").get_json()
    assert d["nuvem"]["banco_ms"] >= 0 and d["escolas"][0]["nome"] == "Recreio"
    assert d["escolas"][0]["catracas"][0]["nome"] == "Catraca"
    inc_id = alertas.abrir("teste", "Teste manual")
    c.post(f"/painel/monitor/{inc_id}/assumir")
    assert db.query("SELECT reconhecido_por FROM incidentes WHERE id = %s", (inc_id,), one=True)["reconhecido_por"] == "Admin"
    c.post(f"/painel/monitor/{inc_id}/encerrar")
    assert c.get("/painel/monitor/estado").get_json()["recentes"][0]["texto"] == "Teste manual"
