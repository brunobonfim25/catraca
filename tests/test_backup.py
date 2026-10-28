"""Cópia semanal criptografada do banco (fora do Railway) e a restauração dela."""
import os

import psycopg2
import pytest

from app import backup, db
from app.config import TestConfig
from app.pessoas import service as pessoas
from tests.conftest import JPEG_OK

SENHA = "senha-do-backup-teste"
DESTINO = TestConfig.DATABASE_URL.rsplit("/", 1)[0] + "/nexosix_test_restore"


def _banco_vazio():
    conn = psycopg2.connect(TestConfig.DATABASE_URL.rsplit("/", 1)[0] + "/postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP DATABASE IF EXISTS nexosix_test_restore")
    cur.execute("CREATE DATABASE nexosix_test_restore")
    conn.close()


def test_ida_e_volta(seed, tmp_path):
    origem = TestConfig.DATABASE_URL
    contagem = {t: db.query(f'SELECT count(*) AS n FROM "{t}"', one=True)["n"] for t in ("pessoas", "pessoa_fotos", "alunos", "consentimentos_lgpd", "fila_sync")}
    proximo_id = db.query("SELECT last_value FROM pg_sequences WHERE sequencename = 'pessoas_id_seq'", one=True)["last_value"]
    resumo, partes = backup.gerar_partes(str(tmp_path), SENHA)
    assert partes and all(open(p, "rb").read(5) == b"NXBK1" or i for i, p in enumerate(partes))
    assert b"Maria Silva" not in open(partes[0], "rb").read()  # criptografado de verdade
    _banco_vazio()
    try:
        db.init(DESTINO)
        with pytest.raises(backup.BackupErro, match="senha errada"):
            backup.restaurar(partes, "outra-senha-qualquer", log_fn=lambda m: None)
        meta = backup.restaurar(partes, SENHA, log_fn=lambda m: None)
        assert meta["versao"] == resumo["versao"]
        for t, n in contagem.items():
            assert db.query(f'SELECT count(*) AS n FROM "{t}"', one=True)["n"] == n, t
        assert pessoas.obter_foto(seed["maria"]["pessoa_id"]) == JPEG_OK
        # sequência continua de onde parou: o próximo cadastro não colide
        novo = db.execute("INSERT INTO pessoas (unidade_id, tipo, nome) VALUES (%s,'visitante','Depois') RETURNING id", (seed["unidade"]["id"],), returning=True)["id"]
        assert novo > proximo_id
        # banco com cadastro: recusa sem --forcar
        with pytest.raises(backup.BackupErro, match="já tem cadastro"):
            backup.restaurar(partes, SENHA, log_fn=lambda m: None)
    finally:
        db.init(origem)


def test_arquivo_cortado_e_recusado(seed, tmp_path):
    _, partes = backup.gerar_partes(str(tmp_path), SENHA)
    with open(partes[-1], "rb+") as f:
        f.truncate(os.path.getsize(partes[-1]) - 100)
    _banco_vazio()
    origem = TestConfig.DATABASE_URL
    try:
        db.init(DESTINO)
        with pytest.raises(backup.BackupErro):
            backup.restaurar(partes, SENHA, log_fn=lambda m: None)
    finally:
        db.init(origem)


def test_envio_semanal(seed, monkeypatch):
    monkeypatch.setenv("BACKUP_SENHA", SENHA)
    monkeypatch.setenv("BACKUP_TELEGRAM_CHAT", "42")
    enviados = []
    assert backup.talvez_enviar(enviar=lambda chat, caminho, legenda: enviados.append((chat, os.path.getsize(caminho), legenda)))
    assert enviados and enviados[0][0] == "42" and "parte 1/" in enviados[0][2]
    assert not backup.talvez_enviar(enviar=lambda *a: enviados.append(a))  # 7 dias ainda não passaram
    db.execute("UPDATE sistema_status SET atualizado_em = now() - interval '8 days' WHERE chave = 'backup:externo'")

    def falha(*a):
        raise RuntimeError("Telegram fora")

    assert not backup.talvez_enviar(enviar=falha)
    from app import alertas

    assert "backup" in alertas.problemas()
    # sem senha configurada: não faz nada (nem alerta)
    monkeypatch.delenv("BACKUP_SENHA")
    assert not backup.talvez_enviar(enviar=falha)


def test_senha_curta_recusada():
    with pytest.raises(backup.BackupErro):
        backup._chave("curta", b"x" * 16)
