"""Auditoria de segurança de 30/10/2026: cada teste é um achado corrigido (bug = caso na suíte)."""
import io
import zipfile

import pytest

from app import create_app, db
from app.alertas import verificacoes
from app.config import TestConfig
from app.pessoas import importacao as imp
from app.pessoas import service as pessoas


def _recepcao(app):
    c = app.test_client()
    c.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"})
    return c


def test_recepcao_nao_cadastra_responsavel_nem_foto(app, seed):
    """Recepção podia criar um "responsável", pôr a foto de qualquer adulto e ele retirava a criança."""
    c = _recepcao(app)
    assert c.post("/pessoas/responsaveis/novo", data={"nome": "Intruso", "aluno_id": seed["maria"]["aluno_id"], "regra": "sempre"}).status_code == 403
    assert c.post(f"/pessoas/{seed['maria']['pessoa_id']}/vincular", data={"responsavel_id": seed["ana"]["responsavel_id"], "regra": "sempre"}).status_code == 403
    assert c.post(f"/pessoas/{seed['maria']['pessoa_id']}/foto", json={"imagem": "x"}).status_code == 403
    assert not db.query("SELECT 1 FROM pessoas WHERE nome = 'Intruso'", one=True)
    # o que é dela continua: ver quem está dentro e liberar manualmente
    assert c.get("/painel/recepcao").status_code == 200


def test_exportacao_neutraliza_formula_no_csv(cliente, seed):
    """Nome "=HYPERLINK(...)" virava fórmula no Excel de quem exporta (CSV injection)."""
    pessoas.criar_aluno(seed["unidade"]["id"], '=HYPERLINK("http://x";"abrir")', seed["turma"]["id"])
    r = cliente.get(f"/unidades/{seed['unidade']['id']}/exportar.zip")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        texto = z.read("alunos.csv").decode("utf-8-sig")
    assert "'=HYPERLINK" in texto and '\n=HYPERLINK' not in texto


def test_ip_da_catraca_validado_e_senha_reexigida(cliente, seed):
    """O campo IP aceitava qualquer host e o agente mandava a senha admin da catraca pra lá."""
    d = seed["dispositivo"]
    base = {"nome": "Catraca", "usuario_api": "admin", "ativo": "1", "porta": "80"}
    for ruim in ("atacante.example.com/x?#", "169.254.169.254", "0.0.0.0", "localhost", "::1", "224.0.0.1"):
        r = cliente.post(f"/dispositivos/{d['id']}/editar", data={**base, "ip": ruim, "senha_api": "s"})
        assert r.status_code == 400, ruim
    # trocar o IP sem digitar a senha de novo: recusado (senão a senha gravada vai pro host novo)
    assert cliente.post(f"/dispositivos/{d['id']}/editar", data={**base, "ip": "192.168.0.77"}).status_code == 400
    assert db.query("SELECT ip FROM dispositivos WHERE id = %s", (d["id"],), one=True)["ip"] == d["ip"]
    r = cliente.post(f"/dispositivos/{d['id']}/editar", data={**base, "ip": "192.168.0.77", "senha_api": "nova"})
    assert r.status_code == 302
    assert db.query("SELECT ip, porta FROM dispositivos WHERE id = %s", (d["id"],), one=True)["ip"] == "192.168.0.77"
    assert cliente.post(f"/dispositivos/{d['id']}/editar", data={**base, "ip": "192.168.0.77", "porta": "99999"}).status_code == 400


def test_miniatura_da_foto_fica_na_auditoria(cliente, seed):
    """?mini=1 devolvia a foto inteira sem registro: dava pra baixar todas as fotos sem rastro."""
    from tests.conftest import JPEG_OK

    pessoas.salvar_foto(seed["maria"]["pessoa_id"], JPEG_OK)
    db.execute("DELETE FROM auditoria")
    assert cliente.get(f"/pessoas/{seed['maria']['pessoa_id']}/foto.jpg?mini=1").status_code == 200
    cliente.get(f"/pessoas/{seed['maria']['pessoa_id']}/foto.jpg?mini=1")
    acoes = [a["acao"] for a in db.query("SELECT acao FROM auditoria ORDER BY id")]
    assert acoes == ["ver_fotos_lista"]  # uma linha por sessão e hora, não uma por miniatura


def test_trocar_telefone_desvincula_telegram_antigo(cliente, seed):
    """Número trocado (separação, celular vendido) e o chat antigo seguia recebendo os avisos."""
    rid, pid = seed["ana"]["responsavel_id"], seed["ana"]["pessoa_id"]
    db.execute("UPDATE responsaveis SET telefone = '21999990001', telegram_chat_id = 555, telegram_token = 'tk' WHERE id = %s", (rid,))
    assert pessoas.atualizar_responsavel(pid, "Ana Silva", "(21) 99999-0001") is False  # mesmo número: mantém
    assert db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (rid,), one=True)["telegram_chat_id"] == 555
    assert pessoas.atualizar_responsavel(pid, "Ana Silva", "(21) 98888-0002") is True
    r = db.query("SELECT telegram_chat_id, telegram_token FROM responsaveis WHERE id = %s", (rid,), one=True)
    assert r["telegram_chat_id"] is None and r["telegram_token"] is None
    assert db.query("SELECT 1 FROM auditoria WHERE acao = 'editar' AND depois->>'telegram_desvinculado' = 'true'", one=True)


def test_planilha_bomba_e_recusada():
    """Um .xlsx de poucos KB pode ter GB dentro (zip bomb) e derrubar o web."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", b"0" * (70 * 1024 * 1024))
    with pytest.raises(imp.PlanilhaInvalida, match="grande demais"):
        imp.ler_arquivo("alunos.xlsx", buf.getvalue())
    with pytest.raises(imp.PlanilhaInvalida):
        imp.ler_arquivo("alunos.xlsx", b"PK\x03\x04lixo")
    # célula e colunas cortadas
    cab, linhas, _ = imp.ler_arquivo("a.csv", ("Aluno;Turma;" + ";".join(f"c{i}" for i in range(200)) + "\n" + "x" * 5000 + ";Pré 1\n").encode())
    assert len(cab) <= imp.MAX_COLUNAS and len(linhas[0][0]) == imp.MAX_CELULA


class _Nuvem(TestConfig):
    MODO = "nuvem"


def test_ping_malformado_nao_derruba_os_alertas(app, seed):
    """desvio_relogio_seg "x" num ping fazia alertas.verificar() morrer pra todas as escolas."""
    uid = seed["unidade"]["id"]
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "tok-x"}' WHERE id = %s""", (uid,))
    c = create_app(_Nuvem).test_client()
    h = {"X-Espelho-Token": "tok-x", "X-Agente-Id": "agente-a"}
    r = c.post("/api/espelho/ping", json={"desvio_relogio_seg": "x", "schema": 1, "web_ok": "nao", "cursor": "c" * 5000, "versao": "v" * 500}, headers=h)
    assert r.status_code == 200
    ag = db.query("SELECT config->'agente' AS a FROM unidades WHERE id = %s", (uid,), one=True)["a"]
    assert ag["desvio_relogio_seg"] is None and ag["schema"] == "1" and ag["web_ok"] is None and ag["cursor"] is None and len(ag["versao"]) == 40
    assert isinstance(verificacoes(), list)


def test_agente_nao_recebe_cpf_contatos_nem_token_do_telegram(outbox_ligado, sincronizado):
    """LGPD: o PC da escola pode ser roubado; CPF, telefone, e-mail e token de ativação ficam na nuvem."""
    from app.espelho import service as espelho

    uid = sincronizado["unidade"]["id"]
    db.execute("UPDATE pessoas SET documento = '123.456.789-00' WHERE id = %s", (sincronizado["ana"]["pessoa_id"],))
    db.execute("UPDATE responsaveis SET telefone = '21999990001', email = 'ana@x.com', telegram_chat_id = 7, telegram_token = 'tk' WHERE id = %s", (sincronizado["ana"]["responsavel_id"],))
    s = espelho.snapshot(sincronizado["unidade"])
    por = {}
    for i in s["itens"]:
        por.setdefault(i["tabela"], []).append(i["dados"])
    assert all("documento" not in d for d in por["pessoas"])
    assert all(not ({"telefone", "email", "telegram_chat_id", "telegram_token"} & set(d)) for d in por["responsaveis"])
    assert all(d["email"].endswith("@oculto.invalid") and "senha_hash" not in d for d in por["usuarios_admin"])
    # o mesmo vale pro que desce depois, pelo reverso
    db.execute("DELETE FROM espelho_outbox")
    db.contexto.unidade_id = uid
    db.execute("UPDATE responsaveis SET telefone = '21999990002' WHERE id = %s", (sincronizado["ana"]["responsavel_id"],))
    db.execute("INSERT INTO auditoria (unidade_id, acao, entidade, entidade_id) VALUES (%s, 'x', 'pessoa', '1')", (uid,))
    lote = espelho.lote_para_agente(sincronizado["unidade"])
    tabelas = {i["tabela"] for i in lote["itens"]}
    assert "responsaveis" in tabelas and "auditoria" not in tabelas
    assert all("telefone" not in i["dados"] for i in lote["itens"] if i["tabela"] == "responsaveis")


def test_agente_nao_reescreve_liberacao_manual(seed):
    """Um PC comprometido não pode mudar quem autorizou nem a justificativa de uma liberação manual."""
    from app.espelho import service as espelho
    from app.eventos import service as eventos

    uid = seed["unidade"]["id"]
    db.execute("""UPDATE unidades SET config = config || '{"espelho_token": "tok-x"}' WHERE id = %s""", (uid,))
    ev = eventos.registrar(uid, "SAIDA_ANTECIPADA", eventos.agora_utc(), dispositivo_id=seed["dispositivo"]["id"], pessoa_id=seed["maria"]["pessoa_id"],
                           sentido="saida", origem="manual", autorizado_por=seed["admin"]["id"], justificativa="consulta")
    dados = dict(ev)
    dados["ocorrido_em"] = dados["ocorrido_em"].isoformat()
    dados["justificativa"] = "nada"
    dados["autorizado_por"] = None
    unidade = db.query("SELECT * FROM unidades WHERE id = %s", (uid,), one=True)
    aplicados, rejeitados = espelho.aplicar_lote(unidade, [{"id": 1, "tabela": "eventos", "operacao": "upsert", "chave": {"id": ev["id"]}, "dados": dados}], lado="nuvem")
    assert rejeitados == 1
    assert db.query("SELECT justificativa FROM eventos WHERE id = %s", (ev["id"],), one=True)["justificativa"] == "consulta"


def test_backup_so_em_chat_dedicado_e_com_retencao(seed, monkeypatch):
    """O backup (fotos de todas as escolas) caía no grupo de alertas e ficava lá para sempre."""
    from app import backup

    monkeypatch.setenv("BACKUP_SENHA", "senha-muito-longa-123")
    monkeypatch.setenv("ALERTA_TELEGRAM_CHAT", "99")
    monkeypatch.delenv("BACKUP_TELEGRAM_CHAT", raising=False)
    assert backup.configuracao()[0] == ""  # sem fallback pro grupo de alertas
    monkeypatch.setenv("BACKUP_TELEGRAM_CHAT", "42")
    antigos = [{"em": f"2026-0{i}-01", "chat": "42", "mensagens": [100 + i]} for i in range(1, 9)]
    import json as _json
    db.execute("INSERT INTO sistema_status (chave, valor, atualizado_em) VALUES ('backup:externo', %s, now() - interval '8 days')",
               (_json.dumps({"historico": antigos}),), outbox=False)
    apagadas = []
    monkeypatch.setattr(backup, "_telegram_apagar", lambda chat, mid: apagadas.append((chat, mid)))
    contador = iter(range(500, 600))
    backup.enviar_agora(enviar=lambda chat, caminho, legenda: next(contador))
    assert apagadas == [("42", 101)]  # só o mais velho sai; ficam os 8 últimos
    hist = db.query("SELECT valor FROM sistema_status WHERE chave = 'backup:externo'", one=True)["valor"]["historico"]
    assert len(hist) == 8 and hist[-1]["mensagens"][0] == 500
