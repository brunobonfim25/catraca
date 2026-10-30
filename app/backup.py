"""Cópia semanal do banco da nuvem FORA do Railway (decisão de 28/10/2026).

Por quê: o backup do próprio Railway mora no mesmo volume — se o volume for apagado, os
backups vão junto. Esta cópia sai do Railway, criptografada, para o Telegram do Bruno.

Como:
  1. gera: COPY binário de cada tabela num único snapshot (REPEATABLE READ) + a versão das
     migrations e o valor das sequências, tudo num .tar.gz (não depende do pg_dump, que não
     existe no container do Railway);
  2. cifra: AES-256-GCM em blocos de 4 MB, chave derivada da BACKUP_SENHA (scrypt). A senha
     só existe no Railway e com o Bruno: sem ela o arquivo é lixo. Fotos de criança (dado
     biométrico) nunca saem em claro;
  3. divide em partes de 45 MB (o bot do Telegram aceita até 50 MB por arquivo) e manda.
Restaurar: `flask restaurar-backup parte01 parte02 …` num banco VAZIO (ver README).
"""
import io
import json
import logging
import os
import struct
import tarfile
import tempfile
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from psycopg2.extras import Json

from . import db

log = logging.getLogger(__name__)

MAGICO = b"NXBK1"
BLOCO = 4 * 1024 * 1024
PARTE_MAX = 45 * 1024 * 1024
INTERVALO_DIAS = 7
# Operacionais/regeráveis: não vale a pena guardar (o outbox pode ser grande)
FORA = {"espelho_outbox", "login_falhas", "sistema_status", "schema_migrations"}


class BackupErro(Exception):
    pass


# ---------------------------------------------------------------- gerar

def tabelas():
    return [r["tablename"] for r in db.query("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            if r["tablename"] not in FORA]


def gerar(destino):
    """Escreve o .tar.gz em `destino` (arquivo binário aberto). Devolve o resumo."""
    resumo = {"tabelas": {}}
    with tarfile.open(fileobj=destino, mode="w:gz") as tar:
        with db.transacao(outbox=False) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # tudo do mesmo instante
            cur.execute("SELECT max(versao) AS v FROM schema_migrations")
            resumo["versao"] = cur.fetchone()["v"]
            cur.execute("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'")
            resumo["sequencias"] = {r["sequencename"]: r["last_value"] for r in cur.fetchall()}
            for t in tabelas():
                cur.execute(f'SELECT count(*) AS n FROM "{t}"')
                resumo["tabelas"][t] = cur.fetchone()["n"]
                with tempfile.TemporaryFile() as tmp:
                    cur.copy_expert(f'COPY "{t}" TO STDOUT WITH (FORMAT binary)', tmp)
                    info = tarfile.TarInfo(f"{t}.copy")
                    info.size = tmp.tell()
                    tmp.seek(0)
                    tar.addfile(info, tmp)
        resumo["criado_em"] = datetime.now(timezone.utc).isoformat()
        meta = json.dumps(resumo, ensure_ascii=False, indent=1).encode()
        info = tarfile.TarInfo("meta.json")
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
    return resumo


# ---------------------------------------------------------------- cifrar / decifrar

def _chave(senha, sal):
    if not senha or len(senha) < 12:
        raise BackupErro("BACKUP_SENHA precisa de 12 caracteres ou mais")
    return Scrypt(salt=sal, length=32, n=2 ** 15, r=8, p=1).derive(senha.encode())


def cifrar(origem, destino, senha):
    """Blocos de 4 MB, cada um com nonce próprio; o último é marcado (um arquivo cortado no
    meio não passa por inteiro)."""
    sal, prefixo = os.urandom(16), os.urandom(8)
    aes = AESGCM(_chave(senha, sal))
    destino.write(MAGICO + sal + prefixo)
    i, atual = 0, origem.read(BLOCO)
    while True:
        proximo = origem.read(BLOCO)
        ultimo = not proximo
        nonce = prefixo + struct.pack(">I", i)
        dado = aes.encrypt(nonce, atual, b"%d|%d" % (i, ultimo))
        destino.write(struct.pack(">I", len(dado)) + dado)
        if ultimo:
            return
        i, atual = i + 1, proximo


def decifrar(origem, destino, senha):
    cab = origem.read(len(MAGICO) + 24)
    if not cab.startswith(MAGICO):
        raise BackupErro("não é um backup da Nexosix (ou as partes estão fora de ordem)")
    sal, prefixo = cab[len(MAGICO):len(MAGICO) + 16], cab[len(MAGICO) + 16:]
    aes = AESGCM(_chave(senha, sal))
    i = 0
    while True:
        tam = origem.read(4)
        if not tam:
            raise BackupErro("backup incompleto: falta a última parte")
        dado = origem.read(struct.unpack(">I", tam)[0])
        nonce = prefixo + struct.pack(">I", i)
        for ultimo in (0, 1):
            try:
                destino.write(aes.decrypt(nonce, dado, b"%d|%d" % (i, ultimo)))
                break
            except Exception:  # noqa: BLE001 — tenta como último bloco
                if ultimo:
                    raise BackupErro("senha errada ou arquivo corrompido") from None
        if ultimo:
            return
        i += 1


def dividir(caminho, pasta, nome):
    partes, n = [], 0
    with open(caminho, "rb") as f:
        while True:
            pedaco = f.read(PARTE_MAX)
            if not pedaco:
                return partes
            n += 1
            p = os.path.join(pasta, f"{nome}.parte{n:02d}")
            with open(p, "wb") as out:
                out.write(pedaco)
            partes.append(p)


# ---------------------------------------------------------------- restaurar

def restaurar(partes, senha, log_fn=print, forcar=False):
    """Banco VAZIO (só migrations) ← backup. Migra até a versão do backup, carrega os dados e
    depois aplica as migrations mais novas do código."""
    if not forcar and db.query("SELECT to_regclass('public.pessoas') IS NOT NULL AS t", one=True)["t"]:
        if db.query("SELECT EXISTS (SELECT 1 FROM pessoas) AS t", one=True)["t"]:
            raise BackupErro("o banco de destino já tem cadastro; restaure num banco vazio (ou use --forcar)")
    with tempfile.TemporaryDirectory() as pasta:
        cifrado = os.path.join(pasta, "b.nxbk")
        with open(cifrado, "wb") as out:
            for p in partes:
                with open(p, "rb") as f:
                    while pedaco := f.read(BLOCO):
                        out.write(pedaco)
        claro = os.path.join(pasta, "b.tar.gz")
        with open(cifrado, "rb") as f, open(claro, "wb") as out:
            decifrar(f, out, senha)
        with tarfile.open(claro, "r:gz") as tar:
            meta = json.load(tar.extractfile("meta.json"))
            db.migrar(log=log_fn, ate=meta["versao"])
            with db.transacao(outbox=False) as cur:
                cur.execute("SET LOCAL session_replication_role = replica")  # sem FKs/triggers durante a carga
                existentes = [t for t in meta["tabelas"] if db.query("SELECT to_regclass(%s) IS NOT NULL AS t", (f"public.{t}",), one=True)["t"]]
                cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in existentes) + " CASCADE")
                for t in existentes:
                    cur.copy_expert(f'COPY "{t}" FROM STDIN WITH (FORMAT binary)', tar.extractfile(f"{t}.copy"))
                    log_fn(f"{t}: {meta['tabelas'][t]} linha(s)")
                for seq, valor in meta["sequencias"].items():
                    if valor is not None:
                        cur.execute("SELECT setval(%s, %s, true)", (seq, valor))
        novas = db.migrar(log=log_fn)
    log_fn(f"restaurado o backup de {meta['criado_em']} (versão {meta['versao']}); {novas} migration(s) nova(s) aplicada(s) depois")
    return meta


# ---------------------------------------------------------------- envio semanal

def _telegram_documento(chat, caminho, legenda):
    import requests

    from .notificacoes.telegram import API, TelegramErro, _cfg

    with open(caminho, "rb") as f:
        r = requests.post(API.format(token=_cfg("TELEGRAM_BOT_TOKEN"), metodo="sendDocument"),
                          data={"chat_id": chat, "caption": legenda}, files={"document": (os.path.basename(caminho), f)}, timeout=300)
    dados = r.json() if r.content else {}
    if not r.ok or not dados.get("ok"):
        raise TelegramErro(r.status_code, dados.get("description") or r.text[:200])
    return (dados.get("result") or {}).get("message_id")


def _telegram_apagar(chat, message_id):
    import requests

    from .notificacoes.telegram import API, _cfg

    try:
        requests.post(API.format(token=_cfg("TELEGRAM_BOT_TOKEN"), metodo="deleteMessage"), json={"chat_id": chat, "message_id": message_id}, timeout=30)
    except Exception:  # noqa: BLE001 — retenção é melhor esforço; não derruba o envio novo
        log.warning("não consegui apagar a parte antiga %s do backup no Telegram", message_id)


# Auditoria de 30/10 (LGPD): o backup tem as fotos de todas as escolas. Vai só para um chat
# dedicado (nunca o grupo de alertas, que tem mais gente) e as cópias antigas são apagadas de lá.
RETENCAO_BACKUPS = 8


def configuracao():
    chat = os.environ.get("BACKUP_TELEGRAM_CHAT") or ""
    return chat, os.environ.get("BACKUP_SENHA", "")


def gerar_partes(pasta, senha, nome=None):
    nome = nome or f"nexosix-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
    claro, cifrado = os.path.join(pasta, "b.tar.gz"), os.path.join(pasta, "b.nxbk")
    with open(claro, "wb") as f:
        resumo = gerar(f)
    with open(claro, "rb") as f, open(cifrado, "wb") as out:
        cifrar(f, out, senha)
    os.remove(claro)
    partes = dividir(cifrado, pasta, nome)
    os.remove(cifrado)
    return resumo, partes


def enviar_agora(enviar=None):
    chat, senha = configuracao()
    if not chat or not senha:
        raise BackupErro("configure BACKUP_SENHA e BACKUP_TELEGRAM_CHAT (um chat só para isto) no worker")
    enviar = enviar or _telegram_documento
    with tempfile.TemporaryDirectory() as pasta:
        resumo, partes = gerar_partes(pasta, senha)
        total = sum(os.path.getsize(p) for p in partes)
        mensagens = []
        for i, p in enumerate(partes, 1):
            mid = enviar(chat, p, f"Backup Nexosix {resumo['criado_em'][:10]} — parte {i}/{len(partes)} "
                                  f"({sum(resumo['tabelas'].values())} linhas). Guarde todas as partes; a senha está com você.")
            if mid:
                mensagens.append(mid)
    anterior = (ultimo() or {}).get("valor") or {}
    historico = list(anterior.get("historico") or []) + [{"em": resumo["criado_em"], "chat": str(chat), "mensagens": mensagens}]
    for antigo in historico[:-RETENCAO_BACKUPS]:
        for mid in antigo.get("mensagens") or []:
            _telegram_apagar(antigo.get("chat") or chat, mid)
    historico = historico[-RETENCAO_BACKUPS:]
    db.execute("INSERT INTO sistema_status (chave, valor, atualizado_em) VALUES ('backup:externo', %s, now()) "
               "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now()",
               (Json({"partes": len(partes), "bytes": total, "versao": resumo["versao"], "erro": None, "historico": historico}),), outbox=False)
    log.info("backup externo enviado: %d parte(s), %.1f MB", len(partes), total / 1e6)
    return len(partes), total


def ultimo():
    return db.query("SELECT valor, atualizado_em FROM sistema_status WHERE chave = 'backup:externo'", one=True)


def talvez_enviar(enviar=None):
    """Worker da nuvem: manda se o último foi há 7+ dias (falhou: tenta de novo em 6 h)."""
    chat, senha = configuracao()
    if not chat or not senha:
        return False
    u = ultimo()
    agora = datetime.now(timezone.utc)
    if u:
        dias = (agora - u["atualizado_em"]).total_seconds() / 86400
        falhou = (u["valor"] or {}).get("erro")
        if (not falhou and dias < INTERVALO_DIAS) or (falhou and dias * 24 < 6):
            return False
    try:
        enviar_agora(enviar)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("backup externo falhou")
        anterior = (u["valor"] if u else {}) or {}
        db.execute("INSERT INTO sistema_status (chave, valor, atualizado_em) VALUES ('backup:externo', %s, now()) "
                   "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now()",
                   (Json({**anterior, "erro": str(e)[:300], "ultimo_ok": anterior.get("ultimo_ok") or (u and u["atualizado_em"].isoformat())}),), outbox=False)
        return False
