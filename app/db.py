"""Pool psycopg2, helpers de consulta e runner de migrations.

Uso:
    from app import db
    rows = db.query("SELECT * FROM turmas WHERE unidade_id = %s", (uid,))
    row  = db.query("...", (..,), one=True)
    novo = db.execute("INSERT ... RETURNING id", (..,), returning=True)

Cada chamada pega uma conexão do pool, faz commit e devolve. Pra transação com várias
instruções use `with db.transacao() as cur:`.
"""
import os
import re
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pgpool

_pool = None
_lock = threading.Lock()
_url = None
# Parâmetros de sessão aplicados em toda transação (nexosix.outbox='on' liga os triggers do
# espelho). `gucs_fn` são funções que devolvem parâmetros dinâmicos (ex.: nexosix.unidade = a
# unidade da requisição ou do job do worker, pra nuvem saber de quem é cada linha do outbox).
gucs = {}
gucs_fn = []
contexto = threading.local()  # contexto.unidade_id, usado pelo worker fora de requisição


def init(database_url, minconn=1, maxconn=10):
    """Cria o pool. Pode ser chamado de novo com outra URL (testes)."""
    global _pool, _url
    with _lock:
        if _pool is not None and _url == database_url:
            return
        if _pool is not None:
            _pool.closeall()
        _url = database_url
        _pool = pgpool.ThreadedConnectionPool(minconn, maxconn, database_url)
        psycopg2.extras.register_default_jsonb(globally=True)


def fechar():
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def conexao():
    """Conexão crua do pool; devolve ao final (fecha se estiver quebrada)."""
    if _pool is None:
        raise RuntimeError("db.init() não foi chamado")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        # Conexão com erro de transação pendente (ou morta) fica inutilizável: melhor descartar.
        try:
            quebrada = conn.closed or conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR
        except psycopg2.Error:
            quebrada = True
        if quebrada:
            try:
                conn.rollback()
            except Exception:
                pass
            _pool.putconn(conn, close=True)
        else:
            _pool.putconn(conn)


@contextmanager
def transacao(outbox=None):
    """Cursor dentro de uma transação. Commit ao sair sem erro, rollback com erro.
    outbox=False desliga o espelho só nesta transação (escritas de controle que não devem
    viajar entre local e nuvem)."""
    with conexao() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            for chave, valor in gucs.items():
                cur.execute(f"SET LOCAL {chave} = %s", (valor,))
            for fn in gucs_fn:
                for chave, valor in (fn() or {}).items():
                    cur.execute(f"SET LOCAL {chave} = %s", (str(valor),))
            if outbox is False:
                cur.execute("SET LOCAL nexosix.outbox = 'off'")
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def _com_retry(fn):
    """Conexão do pool morta (banco reiniciou, SSL caiu) → descarta e tenta uma vez de novo.
    Sem isso a primeira requisição depois de um restart do banco dava 500."""
    try:
        return fn()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        return fn()


def query(sql, params=None, one=False):
    def _run():
        with transacao() as cur:
            cur.execute(sql, params)
            if one:
                return cur.fetchone()
            return cur.fetchall()

    return _com_retry(_run)


def execute(sql, params=None, returning=False, outbox=None):
    """Executa INSERT/UPDATE/DELETE. Com returning=True devolve a primeira linha."""

    def _run():
        with transacao(outbox=outbox) as cur:
            cur.execute(sql, params)
            if returning:
                return cur.fetchone()
            return cur.rowcount

    return _com_retry(_run)


# ---------------------------------------------------------------- migrations

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
_NOME_RE = re.compile(r"^(\d{3})_.+\.sql$")


def listar_migrations(diretorio=_MIGRATIONS_DIR):
    nomes = sorted(n for n in os.listdir(diretorio) if _NOME_RE.match(n))
    return [(n, os.path.join(diretorio, n)) for n in nomes]


def migrar(diretorio=_MIGRATIONS_DIR, log=print):
    """Aplica os SQL numerados ainda não registrados em schema_migrations. Cada arquivo
    roda numa transação própria: se um falhar, os anteriores ficam e o erro aparece."""
    with transacao() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " versao text PRIMARY KEY, aplicado_em timestamptz NOT NULL DEFAULT now())"
        )
    aplicadas = {r["versao"] for r in query("SELECT versao FROM schema_migrations")}
    novas = 0
    for nome, caminho in listar_migrations(diretorio):
        if nome in aplicadas:
            continue
        with open(caminho, encoding="utf-8") as f:
            sql = f.read()
        with transacao() as cur:
            cur.execute(sql)
            cur.execute("INSERT INTO schema_migrations (versao) VALUES (%s)", (nome,))
        log(f"migration aplicada: {nome}")
        novas += 1
    return novas
