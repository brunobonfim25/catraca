"""Sincronização nuvem ↔ agente da escola.

Os dois lados têm o mesmo esquema. Cada escrita vira uma linha em `espelho_outbox` (trigger,
migration 003/004). Quem escreve o quê:
  nuvem  → cadastro (pessoas, fotos, turmas, vínculos, dispositivos, usuários), itens novos
           da fila_sync (fotos pra catraca, comandos), liberações manuais do painel
  agente → eventos, presença, responsável em espera, status de sync por pessoa, status da
           catraca, status dos itens da fila
Aplicar um lote nunca gera outbox (SET LOCAL nexosix.outbox = 'off'), então não há eco.
Ids não colidem: o agente usa a faixa unidade_id × 10 milhões; a nuvem, ids pequenos.
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


# tabela → (chaves primárias, tem coluna unidade_id)
TABELAS = {
    "empresas": (["id"], False), "unidades": (["id"], False), "usuarios_admin": (["id"], False),
    "turmas": (["id"], True), "pessoas": (["id"], True), "pessoa_fotos": (["pessoa_id"], False),
    "alunos": (["id"], False), "responsaveis": (["id"], False), "aluno_responsavel": (["aluno_id", "responsavel_id"], False),
    "funcionarios": (["id"], False), "dispositivos": (["id"], True), "dispositivo_leitores": (["id"], False),
    "dispositivo_pessoa": (["dispositivo_id", "pessoa_id"], False), "fila_sync": (["id"], False), "eventos": (["id"], True),
    "responsavel_em_espera": (["id"], True), "presenca": (["pessoa_id"], True), "fila_notificacao": (["id"], True),
    "auditoria": (["id"], False), "consentimentos_lgpd": (["id"], False), "avisos_retirada": (["id"], True),
}

# Colunas que só o agente atualiza (status da catraca). Quando a nuvem manda a linha de
# dispositivos (editou IP, modo…), essas colunas NÃO sobrescrevem o que o agente sabe.
COLUNAS_DO_AGENTE = {"dispositivos": {"status", "ultimo_contato", "ultimo_log_id", "firmware", "serial", "device_id_fabricante"}}

# O snapshot inicial do agente traz o cadastro da unidade nesta ordem (pais antes de filhos).
SNAPSHOT = [
    ("empresas", "SELECT e.* FROM empresas e JOIN unidades u ON u.empresa_id = e.id WHERE u.id = %(u)s"),
    ("unidades", "SELECT * FROM unidades WHERE id = %(u)s"),
    # eventos.autorizado_por e auditoria apontam pra quem fez; o agente precisa conhecer os usuários
    ("usuarios_admin", "SELECT * FROM usuarios_admin WHERE empresa_id = (SELECT empresa_id FROM unidades WHERE id = %(u)s) OR empresa_id IS NULL"),
    ("turmas", "SELECT * FROM turmas WHERE unidade_id = %(u)s"),
    ("pessoas", "SELECT * FROM pessoas WHERE unidade_id = %(u)s"),
    ("pessoa_fotos", "SELECT f.* FROM pessoa_fotos f JOIN pessoas p ON p.id = f.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("alunos", "SELECT a.* FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("responsaveis", "SELECT r.* FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("aluno_responsavel", "SELECT ar.* FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas p ON p.id = a.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("funcionarios", "SELECT f.* FROM funcionarios f JOIN pessoas p ON p.id = f.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("dispositivos", "SELECT * FROM dispositivos WHERE unidade_id = %(u)s"),
    ("dispositivo_leitores", "SELECT l.* FROM dispositivo_leitores l JOIN dispositivos d ON d.id = l.dispositivo_id WHERE d.unidade_id = %(u)s"),
    ("dispositivo_pessoa", "SELECT dp.* FROM dispositivo_pessoa dp JOIN dispositivos d ON d.id = dp.dispositivo_id WHERE d.unidade_id = %(u)s"),
    ("avisos_retirada", "SELECT * FROM avisos_retirada WHERE unidade_id = %(u)s AND data >= current_date - 1"),
    ("presenca", "SELECT * FROM presenca WHERE unidade_id = %(u)s"),
    ("responsavel_em_espera", "SELECT * FROM responsavel_em_espera WHERE unidade_id = %(u)s AND expira_em > now()"),
    ("fila_sync", "SELECT f.* FROM fila_sync f JOIN dispositivos d ON d.id = f.dispositivo_id WHERE d.unidade_id = %(u)s AND f.status IN ('pendente','processando')"),
]

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
        s = valor[2:] if isinstance(valor, str) and valor.startswith("\\x") else valor  # to_jsonb: "\x..." hex
        return psycopg2.Binary(bytes.fromhex(s))
    if tipo == "ARRAY":
        return list(valor)
    return valor


def _json(obj):
    return json.dumps(obj, default=str)


# ---------------------------------------------------------------- outbox (os dois lados)

def lote_pendente(limite=500, unidade_id=None, desde=0):
    if unidade_id is None:
        rows = db.query("SELECT id, tabela, operacao, chave, dados FROM espelho_outbox WHERE id > %s ORDER BY id LIMIT %s", (desde, limite))
    else:
        rows = db.query("SELECT id, tabela, operacao, chave, dados FROM espelho_outbox WHERE unidade_id = %s AND id > %s ORDER BY id LIMIT %s", (unidade_id, desde, limite))
    return [dict(r) for r in rows]


def confirmar(ids=None, unidade_id=None, ate=None):
    if ids:
        db.execute("DELETE FROM espelho_outbox WHERE id = ANY(%s)", (list(ids),), outbox=False)
    elif unidade_id is not None and ate is not None:
        db.execute("DELETE FROM espelho_outbox WHERE unidade_id = %s AND id <= %s", (unidade_id, ate), outbox=False)


def pendentes():
    return db.query("SELECT count(*) AS n, min(criado_em) AS desde FROM espelho_outbox", one=True)


def limpar_outbox_da_nuvem():
    """Linhas sem unidade (ninguém vai puxar) e linhas velhas demais."""
    db.execute("DELETE FROM espelho_outbox WHERE unidade_id IS NULL AND criado_em < now() - interval '1 day'", outbox=False)
    db.execute("DELETE FROM espelho_outbox WHERE criado_em < now() - interval '30 days'", outbox=False)


# ---------------------------------------------------------------- aplicar (os dois lados)

def _pertence(tabela, dados, unidade):
    pks, tem_unidade = TABELAS[tabela]
    if tem_unidade:
        return int(dados.get("unidade_id") or 0) == unidade["id"]
    if tabela == "unidades":
        return int(dados.get("id") or 0) == unidade["id"]
    if tabela == "empresas":
        return int(dados.get("id") or 0) == unidade["empresa_id"]
    if tabela == "usuarios_admin":
        return dados.get("empresa_id") in (unidade["empresa_id"], None)  # da rede ou superadmin Nexosix
    return True


# O agente não precisa da trilha de auditoria da nuvem (e ela cresce)
IGNORADAS_NO_AGENTE = {"auditoria", "consentimentos_lgpd"}


def aplicar_lote(unidade, itens, lado="nuvem"):
    """Aplica itens vindos do outro lado. Tudo numa transação; nunca gera outbox.
    Devolve (aplicados, rejeitados). `lado` é quem está aplicando ('nuvem' ou 'agente')."""
    aplicados = rejeitados = 0
    with db.transacao(outbox=False) as cur:
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        for item in itens:
            tabela = item.get("tabela")
            if tabela not in TABELAS:
                rejeitados += 1
                continue
            if lado == "agente" and tabela in IGNORADAS_NO_AGENTE:
                aplicados += 1
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
                protegidas = COLUNAS_DO_AGENTE.get(tabela, set()) if lado == "agente" else set()
                sets = []
                for c in nomes:
                    if c in pks or c in protegidas:
                        continue
                    if c == "config" and tabela in ("unidades", "dispositivos"):
                        sets.append(f"config = {tabela}.config || EXCLUDED.config")  # cada lado tem chaves próprias
                    else:
                        sets.append(f"{c} = EXCLUDED.{c}")
                sql = (f"INSERT INTO {tabela} ({', '.join(nomes)}) VALUES ({', '.join(['%s'] * len(nomes))}) "
                       f"ON CONFLICT ({', '.join(pks)}) DO UPDATE SET {', '.join(sets) or pks[0] + ' = EXCLUDED.' + pks[0]}")
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
    return aplicados, rejeitados


# ---------------------------------------------------------------- lado nuvem

def unidade_por_token(token):
    if not token:
        return None
    return db.query("SELECT * FROM unidades WHERE config->>'espelho_token' = %s", (token,), one=True)


def receber_do_agente(unidade, itens):
    aplicados, rejeitados = aplicar_lote(unidade, itens, lado="nuvem")
    _marcar(unidade["id"], {"agente_visto_em": _agora(), "agente_erro": None})
    return aplicados, rejeitados


def snapshot(unidade):
    """Carga inicial do agente: o cadastro inteiro da unidade + até onde o outbox vai."""
    itens = []
    ate = db.query("SELECT COALESCE(max(id), 0) AS m FROM espelho_outbox", one=True)["m"]
    for tabela, sql in SNAPSHOT:
        for r in db.query(sql, {"u": unidade["id"]}):
            dados = json.loads(_json(dict(r)))
            # bytea e jsonb como o to_jsonb do trigger entregaria
            for c, (tipo, _) in colunas(tabela).items():
                if tipo == "bytea" and r.get(c) is not None:
                    dados[c] = "\\x" + bytes(r[c]).hex()
            itens.append({"id": 0, "tabela": tabela, "operacao": "upsert", "chave": {k: dados[k] for k in TABELAS[tabela][0]}, "dados": dados})
    return {"itens": itens, "ate": ate}


def registrar_ping(unidade, dados):
    agente = {k: dados.get(k) for k in ("versao", "base_url", "ip_lan", "outbox_pendentes", "cursor") if k in dados}
    _marcar(unidade["id"], {"agente_visto_em": _agora(), "agente": agente})


def _marcar(unidade_id, campos):
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json(campos), unidade_id), outbox=False)


def _agora():
    return datetime.now(timezone.utc).isoformat()


def agente_silencioso(unidade, segundos=120):
    visto = (unidade.get("config") or {}).get("agente_visto_em")
    if not visto:
        return True
    dt = datetime.fromisoformat(visto)
    return (datetime.now(timezone.utc) - dt).total_seconds() > segundos


# ---------------------------------------------------------------- lado agente

def estado_local():
    return db.query("SELECT * FROM espelho_estado WHERE id = 1", one=True)


def _cursor(valor):
    db.execute("UPDATE espelho_estado SET cursor_nuvem = %s, atualizado_em = now() WHERE id = 1", (valor,), outbox=False)


def _unidade_local(unidade_id):
    return db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True) or {"id": unidade_id, "empresa_id": None}


def _resposta(r, contexto):
    corpo = {}
    try:
        corpo = r.json()
    except ValueError:
        pass
    if not r.ok or (isinstance(corpo, dict) and corpo.get("ok") is False):
        raise EspelhoErro(f"{contexto}: HTTP {r.status_code} {corpo.get('erro') if isinstance(corpo, dict) else r.text[:200]}")
    return corpo


def carga_inicial(nuvem_url, token, unidade_id, get=None):
    """Primeira vez: baixa o snapshot e posiciona o cursor."""
    get = get or requests.get
    try:
        r = get(f"{nuvem_url}/api/espelho/{token}/snapshot", timeout=300)
    except requests.RequestException as e:
        raise EspelhoErro(f"snapshot: sem resposta da nuvem: {e}") from e
    corpo = _resposta(r, "snapshot")
    itens = corpo.get("itens") or []
    # unidade/empresa vêm no próprio snapshot; o _pertence precisa do empresa_id
    uni = next((i["dados"] for i in itens if i["tabela"] == "unidades"), None)
    if not uni or int(uni["id"]) != int(unidade_id):
        raise EspelhoErro("snapshot não trouxe a unidade esperada")
    unidade = {"id": int(uni["id"]), "empresa_id": int(uni["empresa_id"])}
    aplicados, rejeitados = aplicar_lote(unidade, itens, lado="agente")
    _cursor(int(corpo.get("ate") or 0))
    return aplicados


def puxar(nuvem_url, token, unidade_id, get=None, post=None, limite=500, max_lotes=20):
    """Traz o que a nuvem mudou pra esta unidade desde o cursor. Devolve itens aplicados."""
    get, post = get or requests.get, post or requests.post
    unidade = _unidade_local(unidade_id)
    total = 0
    for _ in range(max_lotes):
        cursor = estado_local()["cursor_nuvem"]
        try:
            r = get(f"{nuvem_url}/api/espelho/{token}/reverso", params={"desde": cursor, "limite": limite}, timeout=90)
        except requests.RequestException as e:
            raise EspelhoErro(f"reverso: sem resposta da nuvem: {e}") from e
        itens = _resposta(r, "reverso").get("itens") or []
        if not itens:
            break
        aplicar_lote(unidade, itens, lado="agente")
        ate = itens[-1]["id"]
        _cursor(ate)
        try:
            post(f"{nuvem_url}/api/espelho/{token}/reverso/confirmar", data=_json({"ate": ate}), headers={"Content-Type": "application/json"}, timeout=30)
        except requests.RequestException:
            pass  # confirmação é só limpeza; o cursor local já garante que não reaplica
        total += len(itens)
        if len(itens) < limite:
            break
    return total


def empurrar(nuvem_url, token, post=None, limite=500, max_lotes=20):
    """Sobe o outbox do agente (eventos, presença, status…). Devolve itens confirmados."""
    post = post or requests.post
    total = 0
    for _ in range(max_lotes):
        itens = lote_pendente(limite)
        if not itens:
            break
        try:
            r = post(f"{nuvem_url}/api/espelho/{token}", data=_json({"itens": itens}), headers={"Content-Type": "application/json"}, timeout=90)
        except requests.RequestException as e:
            raise EspelhoErro(f"sem resposta da nuvem: {e}") from e
        _resposta(r, "envio")
        confirmar(ids=[i["id"] for i in itens])
        total += len(itens)
        if len(itens) < limite:
            break
    return total


def ping(nuvem_url, token, dados, post=None):
    post = post or requests.post
    try:
        r = post(f"{nuvem_url}/api/espelho/{token}/ping", data=_json(dados), headers={"Content-Type": "application/json"}, timeout=15)
        return r.ok
    except requests.RequestException:
        return False
