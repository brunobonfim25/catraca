"""Espelho local → nuvem (Modelo 3).

LOCAL: triggers (migration 003) enchem espelho_outbox; `enviar_pendentes` empurra lotes por
HTTPS. Só sai conexão (funciona atrás de NAT); nada entra na rede da escola.
NUVEM: `aplicar_lote` faz upsert/delete na mesma estrutura de tabelas. Idempotente: reenviar
um lote não duplica nada. Ids não colidem entre escolas porque cada servidor local usa uma
faixa de sequence própria (`flask configurar-local`).
"""
import json
import logging
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2.extras import Json

from .. import db

log = logging.getLogger(__name__)


class EspelhoErro(Exception):
    pass


# tabela → (chaves primárias, tem coluna unidade_id). Ordem não importa: o lote é aplicado
# na ordem do outbox com FKs adiadas até o commit.
TABELAS = {
    "empresas": (["id"], False), "unidades": (["id"], False), "usuarios_admin": (["id"], False),
    "turmas": (["id"], True), "pessoas": (["id"], True), "pessoa_fotos": (["pessoa_id"], False),
    "alunos": (["id"], False), "responsaveis": (["id"], False), "aluno_responsavel": (["aluno_id", "responsavel_id"], False),
    "funcionarios": (["id"], False), "dispositivos": (["id"], True), "dispositivo_leitores": (["id"], False),
    "dispositivo_pessoa": (["dispositivo_id", "pessoa_id"], False), "eventos": (["id"], True),
    "responsavel_em_espera": (["id"], True), "presenca": (["pessoa_id"], True), "fila_notificacao": (["id"], True),
    "auditoria": (["id"], False), "consentimentos_lgpd": (["id"], False), "avisos_retirada": (["id"], True),
}

_colunas_cache = {}


def colunas(tabela):
    if tabela not in _colunas_cache:
        rows = db.query("SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s", (tabela,))
        _colunas_cache[tabela] = {r["column_name"]: (r["data_type"], r["udt_name"]) for r in rows}
    return _colunas_cache[tabela]


def _adaptar(valor, tipo, udt):
    """JSON (to_jsonb) → parâmetro psycopg2 do tipo da coluna."""
    if valor is None:
        return None
    if tipo in ("jsonb", "json"):
        return Json(valor)
    if tipo == "bytea":
        # to_jsonb codifica bytea como "\x..." (hex)
        s = valor[2:] if isinstance(valor, str) and valor.startswith("\\x") else valor
        return psycopg2.Binary(bytes.fromhex(s))
    if tipo == "ARRAY":
        return list(valor)
    return valor


# ---------------------------------------------------------------- lado local

def lote_pendente(limite=500):
    rows = db.query("SELECT id, tabela, operacao, chave, dados FROM espelho_outbox ORDER BY id LIMIT %s", (limite,))
    return [dict(r) for r in rows]


def confirmar(ids):
    if ids:
        db.execute("DELETE FROM espelho_outbox WHERE id = ANY(%s)", (list(ids),))


def pendentes():
    return db.query("SELECT count(*) AS n, min(criado_em) AS desde FROM espelho_outbox", one=True)


def enviar_pendentes(nuvem_url, token, limite=500, post=None, max_lotes=20):
    """Empurra até `max_lotes` lotes. Devolve total de itens confirmados. Levanta EspelhoErro
    se a nuvem não aceitou (o outbox fica intacto e tenta na próxima rodada)."""
    if not nuvem_url or not token:
        return 0
    post = post or requests.post
    total = 0
    for _ in range(max_lotes):
        itens = lote_pendente(limite)
        if not itens:
            break
        try:
            r = post(f"{nuvem_url}/api/espelho/{token}", data=json.dumps({"itens": itens}, default=str), headers={"Content-Type": "application/json"}, timeout=90)
        except requests.RequestException as e:
            raise EspelhoErro(f"sem resposta da nuvem: {e}") from e
        corpo = {}
        try:
            corpo = r.json()
        except ValueError:
            pass
        if not r.ok or not corpo.get("ok"):
            raise EspelhoErro(f"nuvem recusou o lote: HTTP {r.status_code} {corpo.get('erro') or r.text[:200]}")
        confirmar([i["id"] for i in itens])
        total += len(itens)
        if len(itens) < limite:
            break
    return total


# ---------------------------------------------------------------- lado nuvem

def _pertence(tabela, dados, unidade):
    """Cada token só pode escrever o que é da sua unidade."""
    pks, tem_unidade = TABELAS[tabela]
    if tem_unidade:
        return int(dados.get("unidade_id") or 0) == unidade["id"]
    if tabela == "unidades":
        return int(dados.get("id") or 0) == unidade["id"]
    if tabela == "empresas":
        return int(dados.get("id") or 0) == unidade["empresa_id"]
    if tabela == "usuarios_admin":
        return dados.get("empresa_id") == unidade["empresa_id"]
    return True


def aplicar_lote(unidade, itens):
    """Aplica na nuvem. Tudo numa transação: ou entra o lote inteiro, ou nada."""
    aplicados = rejeitados = 0
    ultimo = None
    with db.transacao() as cur:
        cur.execute("SET LOCAL nexosix.outbox = 'off'")  # a nuvem não re-espelha
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        for item in itens:
            tabela = item.get("tabela")
            if tabela not in TABELAS:
                rejeitados += 1
                continue
            pks, tem_unidade = TABELAS[tabela]
            cols = colunas(tabela)
            if item.get("operacao") == "upsert":
                dados = item.get("dados") or {}
                if not _pertence(tabela, dados, unidade):
                    rejeitados += 1
                    continue
                nomes = [c for c in dados if c in cols]
                valores = [_adaptar(dados[c], *cols[c]) for c in nomes]
                sets = []
                for c in nomes:
                    if c in pks:
                        continue
                    # config da unidade na nuvem guarda o token do espelho: mescla em vez de sobrescrever
                    if tabela == "unidades" and c == "config":
                        sets.append("config = unidades.config || EXCLUDED.config")
                    else:
                        sets.append(f"{c} = EXCLUDED.{c}")
                sql = f"INSERT INTO {tabela} ({', '.join(nomes)}) VALUES ({', '.join(['%s'] * len(nomes))}) ON CONFLICT ({', '.join(pks)}) DO UPDATE SET {', '.join(sets) or pks[0] + ' = EXCLUDED.' + pks[0]}"
                cur.execute(sql, valores)
            else:
                chave = item.get("chave") or {}
                where = [f"{k} = %s" for k in pks]
                params = [chave.get(k) for k in pks]
                if tem_unidade:
                    where.append("unidade_id = %s")
                    params.append(unidade["id"])
                cur.execute(f"DELETE FROM {tabela} WHERE {' AND '.join(where)}", params)
            aplicados += 1
            ultimo = item.get("id")
        cur.execute(
            "UPDATE unidades SET config = config || %s WHERE id = %s",
            (Json({"espelho_ultimo_em": datetime.now(timezone.utc).isoformat(), "espelho_ultimo_id": ultimo}), unidade["id"]),
        )
    return aplicados, rejeitados


def unidade_por_token(token):
    if not token:
        return None
    return db.query("SELECT * FROM unidades WHERE config->>'espelho_token' = %s", (token,), one=True)
