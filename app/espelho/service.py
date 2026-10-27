"""Sincronização nuvem ↔ agente da escola.

Os dois lados têm o mesmo esquema. Cada escrita vira uma linha em `espelho_outbox` (trigger,
migrations 003/004/009), marcada com a(s) escola(s) da própria linha. Quem escreve o quê:
  nuvem  → cadastro (pessoas, fotos, turmas, vínculos, dispositivos, usuários), itens novos
           da fila_sync (fotos pra catraca, comandos), liberações manuais do painel
  agente → eventos, presença, responsável em espera, status de sync por pessoa, status da
           catraca, status dos itens da fila
A nuvem só ACEITA do agente o que é do agente (REGRAS_DO_AGENTE). Revisão de 27/10/2026: antes
ela aplicava qualquer tabela que viesse — quem tivesse o token de uma escola (está no .env do PC
dela) podia criar superadmin, trocar a senha de alguém ou mexer no cadastro.

Aplicar um lote nunca gera outbox (nexosix.outbox = 'off'), então não há eco.
Ids não colidem: o agente usa a faixa unidade_id × 10 milhões; a nuvem, ids pequenos.
"""
import ipaddress
import json
import logging
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2 import errors as pgerros
from psycopg2.extras import Json

from .. import db

log = logging.getLogger(__name__)


class EspelhoErro(Exception):
    pass


class RecargaNecessaria(EspelhoErro):
    """A nuvem descartou mudanças antigas desta escola: o agente precisa baixar tudo de novo."""


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
COLUNAS_DO_AGENTE = {"dispositivos": {"status", "ultimo_contato", "ultimo_log_id", "coleta_log_id", "log_epoca", "firmware", "serial", "device_id_fabricante"}}
# Chaves de dispositivos.config que o agente escreve (regras criadas na catraca, logs pendentes)
CONFIG_DO_AGENTE = {"logs_no_dispositivo", "regra_liberado_id", "regra_so_entrada_id"}

# O que a nuvem aceita do agente. `colunas` = o que pode ser atualizado numa linha que já existe
# (None = todas); `insere` = pode criar linha nova; `apaga` = pode apagar.
REGRAS_DO_AGENTE = {
    "eventos": {"colunas": None, "insere": True},
    "presenca": {"colunas": None, "insere": True},
    "responsavel_em_espera": {"colunas": None, "insere": True, "apaga": True},
    # Só cria: o envio do Telegram é da nuvem. Reaplicar a linha do agente ('pendente') por cima
    # de uma já enviada fazia a mãe receber o aviso de novo.
    "fila_notificacao": {"colunas": set(), "insere": True},
    "auditoria": {"colunas": set(), "insere": True},
    "dispositivos": {"colunas": COLUNAS_DO_AGENTE["dispositivos"] | {"modo", "config"}, "insere": False},
    "dispositivo_pessoa": {"colunas": {"user_id_no_dispositivo", "foto_sync_status", "regras_sync_status", "erro", "atualizado_em"}, "insere": True, "apaga": True},
    "fila_sync": {"colunas": {"status", "tentativas", "proximo_em", "erro", "concluido_em"}, "insere": True},
}

# Referências que precisam ser da mesma escola (coluna → tipo)
REFERENCIAS = {
    "eventos": {"pessoa_id": "pessoa", "dispositivo_id": "dispositivo", "correlacao_id": "pessoa", "autorizado_por": "usuario"},
    "presenca": {"pessoa_id": "pessoa"},
    "responsavel_em_espera": {"responsavel_id": "responsavel", "dispositivo_id": "dispositivo", "aluno_id": "aluno"},
    "fila_notificacao": {"responsavel_id": "responsavel"},
    "dispositivo_pessoa": {"dispositivo_id": "dispositivo", "pessoa_id": "pessoa"},
    "fila_sync": {"dispositivo_id": "dispositivo", "pessoa_id": "pessoa"},
    "auditoria": {"usuario_id": "usuario"},
    "dispositivos": {"id": "dispositivo"},
}
_SQL_REF = {
    "pessoa": "SELECT 1 FROM pessoas WHERE id = %(v)s AND unidade_id = %(u)s",
    "dispositivo": "SELECT 1 FROM dispositivos WHERE id = %(v)s AND unidade_id = %(u)s",
    "responsavel": "SELECT 1 FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE r.id = %(v)s AND p.unidade_id = %(u)s",
    "aluno": "SELECT 1 FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id WHERE a.id = %(v)s AND p.unidade_id = %(u)s",
    "usuario": "SELECT 1 FROM usuarios_admin WHERE id = %(v)s AND (empresa_id = %(e)s OR empresa_id IS NULL)",
}

# O snapshot inicial do agente traz o cadastro da unidade nesta ordem (pais antes de filhos).
# Fotos ficam de fora (vêm paginadas por /fotos): 3 mil fotos num JSON só derrubava a nuvem.
SNAPSHOT = [
    ("empresas", "SELECT e.* FROM empresas e JOIN unidades u ON u.empresa_id = e.id WHERE u.id = %(u)s"),
    ("unidades", "SELECT * FROM unidades WHERE id = %(u)s"),
    # eventos.autorizado_por e auditoria apontam pra quem fez; o agente precisa conhecer os usuários
    ("usuarios_admin", "SELECT * FROM usuarios_admin WHERE empresa_id = (SELECT empresa_id FROM unidades WHERE id = %(u)s) OR empresa_id IS NULL"),
    ("turmas", "SELECT * FROM turmas WHERE unidade_id = %(u)s"),
    ("pessoas", "SELECT * FROM pessoas WHERE unidade_id = %(u)s"),
    ("alunos", "SELECT a.* FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("responsaveis", "SELECT r.* FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("aluno_responsavel", "SELECT ar.* FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas p ON p.id = a.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("funcionarios", "SELECT f.* FROM funcionarios f JOIN pessoas p ON p.id = f.pessoa_id WHERE p.unidade_id = %(u)s"),
    ("dispositivos", "SELECT * FROM dispositivos WHERE unidade_id = %(u)s"),
    ("dispositivo_leitores", "SELECT l.* FROM dispositivo_leitores l JOIN dispositivos d ON d.id = l.dispositivo_id WHERE d.unidade_id = %(u)s"),
    ("dispositivo_pessoa", "SELECT dp.* FROM dispositivo_pessoa dp JOIN dispositivos d ON d.id = dp.dispositivo_id WHERE d.unidade_id = %(u)s"),
    ("avisos_retirada", "SELECT * FROM avisos_retirada WHERE unidade_id = %(u)s AND data >= current_date - 1"),
    # eventos não vêm no snapshot: o último evento de cada presença fica sem referência
    ("presenca", "SELECT pessoa_id, unidade_id, dentro, desde, NULL::bigint AS ultimo_evento_id FROM presenca WHERE unidade_id = %(u)s"),
    ("responsavel_em_espera", "SELECT * FROM responsavel_em_espera WHERE unidade_id = %(u)s AND expira_em > now()"),
    ("fila_sync", "SELECT f.* FROM fila_sync f JOIN dispositivos d ON d.id = f.dispositivo_id WHERE d.unidade_id = %(u)s AND f.status IN ('pendente','processando')"),
]

# Hash de senha não sai da nuvem: o agente não tem login e o PC da escola pode ser roubado.
SEM_SENHA = "!"  # check_password_hash("!", qualquer) é sempre falso

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


def _agora():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- outbox (os dois lados)

def lote_pendente(limite=500):
    """Agente → nuvem: o que ainda não foi confirmado, em ordem. Confirma-se por id (nunca por
    cursor), então uma linha que só fica visível depois nunca é pulada."""
    rows = db.query("SELECT id, tabela, operacao, chave, dados FROM espelho_outbox ORDER BY id LIMIT %s", (limite,))
    return [dict(r) for r in rows]


def confirmar_ids(ids):
    if ids:
        db.execute("DELETE FROM espelho_outbox WHERE id = ANY(%s)", (list(ids),), outbox=False)


def pendentes():
    return db.query("SELECT count(*) AS n, min(criado_em) AS desde FROM espelho_outbox", one=True)


def limpar_outbox_da_nuvem(dias_max=30):
    """Linhas que ninguém vai puxar. Revisão: antes apagava tudo com mais de 30 dias, inclusive o
    que o agente ainda não tinha recebido — PC desligado nas férias voltava sem o cadastro novo.
    Agora: sem escola ou de escola sem agente → some em 1 dia; de escola com agente, só depois
    de `dias_max`, e aí a escola é marcada pra baixar tudo de novo (recarga)."""
    db.execute("DELETE FROM espelho_outbox WHERE unidade_id IS NULL AND criado_em < now() - interval '1 day'", outbox=False)
    db.execute("DELETE FROM espelho_outbox o USING unidades u WHERE u.id = o.unidade_id AND NOT u.config ? 'espelho_token' "
               "AND o.criado_em < now() - interval '1 day'", outbox=False)
    velhas = db.query("SELECT DISTINCT unidade_id FROM espelho_outbox WHERE unidade_id IS NOT NULL AND criado_em < now() - make_interval(days => %s)", (dias_max,))
    for r in velhas:
        _marcar(r["unidade_id"], {"espelho_recarga": True})
        db.execute("DELETE FROM espelho_outbox WHERE unidade_id = %s", (r["unidade_id"],), outbox=False)
        log.warning("outbox da unidade %s passou de %s dias sem o agente buscar: agente vai recarregar tudo", r["unidade_id"], dias_max)


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


def _referencias_ok(cur, tabela, dados, unidade):
    for coluna, tipo in REFERENCIAS.get(tabela, {}).items():
        valor = dados.get(coluna)
        if valor is None:
            continue
        cur.execute(_SQL_REF[tipo], {"v": valor, "u": unidade["id"], "e": unidade.get("empresa_id")})
        if cur.fetchone() is None:
            return False
    return True


# O agente não precisa da trilha de auditoria da nuvem (e ela cresce)
IGNORADAS_NO_AGENTE = {"auditoria", "consentimentos_lgpd"}


class _Rejeitado(Exception):
    pass


def _aplicar_item(cur, item, unidade, lado):
    """Um item. Levanta _Rejeitado (não é permitido) ou erro do banco (tenta de novo/quarentena)."""
    tabela = item.get("tabela")
    if tabela not in TABELAS:
        raise _Rejeitado(f"tabela desconhecida: {tabela}")
    pks, tem_unidade = TABELAS[tabela]
    do_agente = lado == "nuvem"  # a nuvem está aplicando o que veio do agente
    regra = REGRAS_DO_AGENTE.get(tabela) if do_agente else None
    if do_agente and regra is None:
        raise _Rejeitado(f"o agente não escreve em {tabela}")
    cols = colunas(tabela)

    if item.get("operacao") != "upsert":
        if do_agente and not regra.get("apaga"):
            raise _Rejeitado(f"o agente não apaga {tabela}")
        chave = item.get("chave") or {}
        if any(chave.get(k) is None for k in pks):
            raise _Rejeitado("chave incompleta")
        if do_agente and not _referencias_ok(cur, tabela, chave, unidade):
            raise _Rejeitado(f"{tabela} de outra escola")
        where = [f"{k} = %s" for k in pks]
        params = [chave.get(k) for k in pks]
        if tem_unidade:
            where.append("unidade_id = %s")
            params.append(unidade["id"])
        cur.execute(f"DELETE FROM {tabela} WHERE {' AND '.join(where)}", params)
        return

    dados = dict(item.get("dados") or {})
    if do_agente and tabela == "auditoria":
        dados["unidade_id"] = unidade["id"]  # trilha do agente é sempre da escola dele
    if not _pertence(tabela, dados, unidade):
        raise _Rejeitado(f"{tabela} de outra escola")
    if do_agente and not _referencias_ok(cur, tabela, dados, unidade):
        raise _Rejeitado(f"{tabela} aponta para outra escola")
    if do_agente and tabela == "dispositivos":
        # config: só as chaves que o agente escreve; o resto (IP de VPN, sentidos…) é da nuvem
        dados["config"] = {k: v for k, v in (dados.get("config") or {}).items() if k in CONFIG_DO_AGENTE}
    if tabela == "usuarios_admin":
        dados.pop("senha_hash", None)
        dados["senha_hash"] = SEM_SENHA  # só no INSERT; no UPDATE a senha nunca é tocada

    nomes = [c for c in dados if c in cols]
    valores = [_adaptar(dados[c], *cols[c]) for c in nomes]
    protegidas = COLUNAS_DO_AGENTE.get(tabela, set()) if lado == "agente" else set()
    permitidas = regra.get("colunas") if regra else None
    sets = []
    for c in nomes:
        if c in pks or c in protegidas or (tabela == "usuarios_admin" and c == "senha_hash"):
            continue
        if permitidas is not None and c not in permitidas:
            continue
        if c == "config" and tabela in ("unidades", "dispositivos"):
            sets.append(f"config = {tabela}.config || EXCLUDED.config")  # cada lado tem chaves próprias
        else:
            sets.append(f"{c} = EXCLUDED.{c}")
    conflito = f"ON CONFLICT ({', '.join(pks)}) "
    condicoes = []
    if tem_unidade:
        condicoes.append(f"{tabela}.unidade_id = EXCLUDED.unidade_id")  # nunca sobrescreve linha de outra escola
    if tabela == "presenca":
        # Revisão: a presença do lado que chegou por último ganhava, mesmo sendo mais velha
        # (agente sobe "entrou 7h" depois que a nuvem já sabia "saiu 12h" do failover).
        condicoes.append("presenca.desde <= EXCLUDED.desde")
    if sets:
        conflito += f"DO UPDATE SET {', '.join(sets)}" + (f" WHERE {' AND '.join(condicoes)}" if condicoes else "")
    else:
        conflito += "DO NOTHING"
    insere = regra.get("insere", True) if regra else True
    if not insere:
        # só atualiza linha que já existe (ex.: o agente não cria catraca na nuvem)
        upd = [c for c in nomes if c not in pks and (permitidas is None or c in permitidas)]
        if not upd:
            return
        sets_u = [("config = config || %s" if c == "config" else f"{c} = %s") for c in upd]
        params = [valores[nomes.index(c)] for c in upd] + [dados.get(k) for k in pks]
        where = " AND ".join(f"{k} = %s" for k in pks)
        if tem_unidade:
            where += " AND unidade_id = %s"
            params.append(unidade["id"])
        cur.execute(f"UPDATE {tabela} SET {', '.join(sets_u)} WHERE {where}", params)
        return
    cur.execute(f"INSERT INTO {tabela} ({', '.join(nomes)}) VALUES ({', '.join(['%s'] * len(nomes))}) {conflito}", valores)


def _tentar(cur, item, unidade, lado):
    """Aplica num savepoint. Devolve 'ok' | 'rejeitado' | 'duplicado' | ('erro', exc)."""
    cur.execute("SAVEPOINT item")
    try:
        _aplicar_item(cur, item, unidade, lado)
    except _Rejeitado as e:
        cur.execute("ROLLBACK TO SAVEPOINT item")
        log.warning("espelho (%s, unidade %s): rejeitado %s/%s: %s", lado, unidade["id"], item.get("tabela"), item.get("chave"), e)
        return "rejeitado"
    except psycopg2.Error as e:
        cur.execute("ROLLBACK TO SAVEPOINT item")
        if isinstance(e, pgerros.UniqueViolation) and item.get("tabela") == "eventos":
            # o mesmo log da catraca já chegou pelo outro lado (nuvem em failover e agente depois)
            return "duplicado"
        return ("erro", e)
    cur.execute("RELEASE SAVEPOINT item")
    return "ok"


def _quarentena(cur, item, unidade, lado, erro, quarentena_id=None):
    if quarentena_id:
        cur.execute("UPDATE espelho_quarentena SET tentativas = tentativas + 1, erro = %s, tentado_em = now() WHERE id = %s", (str(erro)[:500], quarentena_id))
        return
    cur.execute("INSERT INTO espelho_quarentena (lado, unidade_id, tabela, operacao, chave, dados, erro) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (lado, unidade["id"], item.get("tabela"), item.get("operacao"), Json(item.get("chave")), Json(item.get("dados")), str(erro)[:500]))


QUARENTENA_MAX_TENTATIVAS = 50


def aplicar_lote(unidade, itens, lado="nuvem"):
    """Aplica itens vindos do outro lado numa transação; nunca gera outbox.
    `lado` é quem está aplicando ('nuvem' = veio do agente; 'agente' = veio da nuvem).
    Item com erro (FK, tipo) não derruba o lote: tenta de novo no fim (o pai pode vir depois
    no mesmo lote) e, se continuar falhando, vai pra quarentena e é tentado nas próximas rodadas.
    Devolve (aplicados, rejeitados)."""
    aplicados = rejeitados = 0
    with db.transacao(outbox=False) as cur:
        pendentes_ = []
        for item in itens:
            if lado == "agente" and item.get("tabela") in IGNORADAS_NO_AGENTE:
                aplicados += 1
                continue
            r = _tentar(cur, item, unidade, lado)
            if r in ("ok", "duplicado"):
                aplicados += 1
            elif r == "rejeitado":
                rejeitados += 1
            else:
                pendentes_.append((item, r[1]))
        # segunda passada: filhos que chegaram antes do pai
        while pendentes_:
            ainda = []
            for item, _ in pendentes_:
                r = _tentar(cur, item, unidade, lado)
                if r in ("ok", "duplicado"):
                    aplicados += 1
                elif r == "rejeitado":
                    rejeitados += 1
                else:
                    ainda.append((item, r[1]))
            if len(ainda) == len(pendentes_):
                for item, erro in ainda:
                    log.warning("espelho (%s, unidade %s): quarentena %s/%s: %s", lado, unidade["id"], item.get("tabela"), item.get("chave"), erro)
                    _quarentena(cur, item, unidade, lado, erro)
                break
            pendentes_ = ainda
        _reaplicar_quarentena(cur, unidade, lado)
    return aplicados, rejeitados


def _reaplicar_quarentena(cur, unidade, lado):
    cur.execute("SELECT * FROM espelho_quarentena WHERE lado = %s AND unidade_id = %s AND tentativas < %s ORDER BY id LIMIT 200",
                (lado, unidade["id"], QUARENTENA_MAX_TENTATIVAS))
    for q in cur.fetchall():
        item = {"tabela": q["tabela"], "operacao": q["operacao"], "chave": q["chave"], "dados": q["dados"]}
        r = _tentar(cur, item, unidade, lado)
        if r in ("ok", "duplicado", "rejeitado"):
            cur.execute("DELETE FROM espelho_quarentena WHERE id = %s", (q["id"],))
        else:
            _quarentena(cur, item, unidade, lado, r[1], quarentena_id=q["id"])


def quarentena(unidade_id=None):
    sql = "SELECT lado, unidade_id, tabela, operacao, chave, erro, tentativas, criado_em FROM espelho_quarentena"
    if unidade_id is None:
        return db.query(sql + " ORDER BY id DESC LIMIT 50")
    return db.query(sql + " WHERE unidade_id = %s ORDER BY id DESC LIMIT 50", (unidade_id,))


# ---------------------------------------------------------------- lado nuvem

AGENTE_ATIVO_SEG = 300


class OutroAgente(EspelhoErro):
    pass


def garantir_monitor_token(unidade_id):
    """Token que a catraca desta escola usa nas URLs do Monitor e do modo Pro (um por escola)."""
    import secrets

    db.execute("UPDATE unidades SET config = config || jsonb_build_object('monitor_token', %s::text) "
               "WHERE id = %s AND NOT config ? 'monitor_token'", (secrets.token_hex(16), unidade_id))
    return db.query("SELECT config->>'monitor_token' AS t FROM unidades WHERE id = %s", (unidade_id,), one=True)["t"]


def unidade_por_monitor_token(token):
    if not token:
        return None
    return db.query("SELECT * FROM unidades WHERE config->>'monitor_token' = %s", (token,), one=True)


def unidade_por_token(token):
    if not token:
        return None
    return db.query("SELECT * FROM unidades WHERE config->>'espelho_token' = %s", (token,), one=True)


def conferir_agente(unidade, agente_id):
    """Um agente por escola. Revisão (E13): dois PCs com o mesmo token (reinstalação num PC
    novo com o velho ainda ligado) disputavam a catraca e a fila; o DNS ficava pulando entre
    os dois IPs. Agora o segundo é recusado enquanto o primeiro der sinal nos últimos 5 min."""
    if not agente_id:
        return
    cfg = unidade.get("config") or {}
    atual = cfg.get("agente_id")
    if atual and atual != agente_id and not agente_silencioso(unidade, AGENTE_ATIVO_SEG):
        raise OutroAgente(f"outro servidor desta escola ({atual[:8]}) está ativo; desligue-o ou espere 5 minutos")
    if atual != agente_id:
        _marcar(unidade["id"], {"agente_id": agente_id})
        unidade["config"] = {**cfg, "agente_id": agente_id}


def receber_do_agente(unidade, itens):
    aplicados, rejeitados = aplicar_lote(unidade, itens, lado="nuvem")
    _marcar(unidade["id"], {"agente_visto_em": _agora(), "agente_erro": None})
    return aplicados, rejeitados


def _linhas_atuais(cur, tabela, chaves):
    """Estado atual (como o to_jsonb do trigger) das linhas com essas chaves."""
    pks = TABELAS[tabela][0]
    expr = "to_jsonb(t) - 'senha_hash'" if tabela == "usuarios_admin" else "to_jsonb(t)"
    if not chaves:
        return {}
    tuplas = ", ".join(["(" + ", ".join(["%s"] * len(pks)) + ")"] * len(chaves))
    params = [c.get(k) for c in chaves for k in pks]
    cur.execute(f"SELECT {expr} AS d FROM {tabela} t WHERE ({', '.join(pks)}) IN ({tuplas})", params)
    return {tuple(r["d"].get(k) for k in pks): r["d"] for r in cur.fetchall()}


LOTE_MAX_BYTES = 4 * 1024 * 1024


def lote_para_agente(unidade, desde_xid="0", desde_id=0, limite=500, max_bytes=LOTE_MAX_BYTES):
    """Nuvem → agente: mudanças desta escola depois do cursor (xid, id).

    Revisão (E2): antes era "id > cursor". Duas transações ao mesmo tempo: a que pegou o id
    menor termina depois; o agente já tinha passado do id dela e a linha nunca descia. Agora só
    sai o que veio de transação já terminada (xid < xmin) e o cursor é o par (xid, id).
    Cada item leva o estado ATUAL da linha (não a foto do momento da escrita): se duas edições
    da mesma linha chegarem fora de ordem, o agente fica com a última de qualquer jeito.
    """
    cfg = unidade.get("config") or {}
    if cfg.get("espelho_recarga"):
        raise RecargaNecessaria("a nuvem descartou mudanças antigas desta escola; refaça a carga inicial")
    with db.transacao(outbox=False) as cur:
        cur.execute(
            "SELECT id, xid::text AS xid, tabela, operacao, chave FROM espelho_outbox "
            "WHERE unidade_id = %s AND (xid, id) > (%s::xid8, %s) AND xid < pg_snapshot_xmin(pg_current_snapshot()) "
            "ORDER BY xid, id LIMIT %s",
            (unidade["id"], str(desde_xid or "0"), int(desde_id or 0), limite),
        )
        linhas = cur.fetchall()
        por_tabela = {}
        for l in linhas:
            if l["tabela"] in TABELAS:
                por_tabela.setdefault(l["tabela"], []).append(l["chave"])
        atuais = {t: _linhas_atuais(cur, t, ch) for t, ch in por_tabela.items()}
    itens, vistos, tamanho, ate, cortado = [], set(), 0, None, False
    for l in linhas:
        if l["tabela"] not in TABELAS:
            ate = {"xid": l["xid"], "id": l["id"]}
            continue
        pks = TABELAS[l["tabela"]][0]
        k = (l["tabela"],) + tuple(l["chave"].get(p) for p in pks)
        if k not in vistos:
            dados = atuais[l["tabela"]].get(k[1:])
            item = ({"id": l["id"], "tabela": l["tabela"], "operacao": "upsert", "chave": l["chave"], "dados": dados} if dados is not None
                    else {"id": l["id"], "tabela": l["tabela"], "operacao": "delete", "chave": l["chave"]})
            n = len(_json(item))
            if itens and tamanho + n > max_bytes:
                cortado = True
                break  # o resto vai no próximo lote (fotos são grandes)
            itens.append(item)
            vistos.add(k)
            tamanho += n
        ate = {"xid": l["xid"], "id": l["id"]}
    return {"itens": itens, "ate": ate, "mais": cortado or len(linhas) == limite}


def confirmar_ate(unidade_id, ate_xid, ate_id):
    db.execute("DELETE FROM espelho_outbox WHERE unidade_id = %s AND (xid, id) <= (%s::xid8, %s)",
               (unidade_id, str(ate_xid), int(ate_id)), outbox=False)


def snapshot(unidade):
    """Carga inicial do agente: o cadastro da unidade (sem fotos) + o cursor onde ele começa.

    Revisão (E3): o "até onde" era o maior id do outbox, lido fora da leitura das tabelas —
    o que mudasse entre uma coisa e outra ficava de fora pra sempre. Agora tudo sai de um
    único snapshot do banco (REPEATABLE READ) e o cursor é o xmin dele: o que não estava
    pronto naquele instante desce depois pelo reverso.
    """
    itens = []
    with db.transacao(outbox=False) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text AS x")
        xmin = cur.fetchone()["x"]
        for tabela, sql in SNAPSHOT:
            expr = "to_jsonb(s) - 'senha_hash'" if tabela == "usuarios_admin" else "to_jsonb(s)"
            cur.execute(f"SELECT {expr} AS d FROM ({sql}) s", {"u": unidade["id"]})
            for r in cur.fetchall():
                d = r["d"]
                itens.append({"id": 0, "tabela": tabela, "operacao": "upsert", "chave": {k: d[k] for k in TABELAS[tabela][0]}, "dados": d})
    if (unidade.get("config") or {}).get("espelho_recarga"):
        _marcar(unidade["id"], {"espelho_recarga": False})
    return {"itens": itens, "cursor": {"xid": xmin, "id": 0}}


def fotos(unidade, apos=0, limite=50):
    """Fotos da escola em páginas (carga inicial)."""
    rows = db.query(
        "SELECT to_jsonb(f) AS d FROM pessoa_fotos f JOIN pessoas p ON p.id = f.pessoa_id "
        "WHERE p.unidade_id = %s AND f.pessoa_id > %s ORDER BY f.pessoa_id LIMIT %s",
        (unidade["id"], int(apos or 0), min(int(limite or 50), 200)),
    )
    return [{"id": 0, "tabela": "pessoa_fotos", "operacao": "upsert", "chave": {"pessoa_id": r["d"]["pessoa_id"]}, "dados": r["d"]} for r in rows]


def ip_privado(ip):
    """O DNS da catraca só aponta pra IP de rede local. Revisão (S6): o ping aceitava qualquer
    coisa como ip_lan e o vigia apontava o nome da catraca pra ele — quem tivesse o token
    mandava a catraca falar com um servidor dele."""
    try:
        a = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local


def registrar_ping(unidade, dados):
    agente = {k: dados.get(k) for k in ("versao", "schema", "base_url", "ip_lan", "outbox_pendentes", "cursor", "web_ok", "catraca_contato", "desvio_relogio_seg") if k in dados}
    if agente.get("ip_lan") is not None and not ip_privado(agente["ip_lan"]):
        log.warning("unidade %s: ping com ip_lan não privado (%s) — ignorado", unidade["id"], agente["ip_lan"])
        agente["ip_lan"] = None
    anterior = (unidade.get("config") or {}).get("agente") or {}
    # Desde quando o site do agente (o que a catraca chama) está fora: o vigia assume depois de um tempo
    if agente.get("web_ok") is False:
        agente["web_falha_desde"] = anterior.get("web_falha_desde") or _agora()
    else:
        agente["web_falha_desde"] = None
    _marcar(unidade["id"], {"agente_visto_em": _agora(), "agente": agente})
    return {"agora": _agora(), "schema_nuvem": versao_schema()}


def versao_schema():
    r = db.query("SELECT max(versao) AS v FROM schema_migrations", one=True)
    return r["v"] if r else None


def _marcar(unidade_id, campos):
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json(campos), unidade_id), outbox=False)


def agente_silencioso(unidade, segundos=120):
    visto = (unidade.get("config") or {}).get("agente_visto_em")
    if not visto:
        return True
    dt = datetime.fromisoformat(visto)
    return (datetime.now(timezone.utc) - dt).total_seconds() > segundos


# ---------------------------------------------------------------- lado agente

def estado_local():
    return db.query("SELECT * FROM espelho_estado WHERE id = 1", one=True)


def _cursor(xid, id_):
    db.execute("UPDATE espelho_estado SET cursor_xid = %s, cursor_nuvem = %s, atualizado_em = now() WHERE id = 1", (str(xid), int(id_)), outbox=False)


def _unidade_local(unidade_id):
    return db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True) or {"id": unidade_id, "empresa_id": None}


def _cabecalhos(token):
    """Token no cabeçalho, nunca na URL. Revisão (S17): URL com token fica em log de acesso
    (Railway, proxy, histórico do navegador); quem lê o log vira o agente da escola."""
    est = estado_local() or {}
    h = {"X-Espelho-Token": token, "Content-Type": "application/json"}
    if est.get("agente_id"):
        h["X-Agente-Id"] = est["agente_id"]
    return h


def _resposta(r, contexto):
    corpo = {}
    try:
        corpo = r.json()
    except ValueError:
        pass
    if r.status_code == 409 and isinstance(corpo, dict) and corpo.get("recarga"):
        raise RecargaNecessaria(corpo.get("erro") or "recarga")
    if not r.ok or (isinstance(corpo, dict) and corpo.get("ok") is False):
        raise EspelhoErro(f"{contexto}: HTTP {r.status_code} {corpo.get('erro') if isinstance(corpo, dict) else r.text[:200]}")
    return corpo


def carga_inicial(nuvem_url, token, unidade_id, get=None):
    """Primeira vez (ou recarga): baixa o cadastro, depois as fotos em páginas, e só então
    marca a carga como feita — se cair no meio, recomeça do zero na próxima rodada."""
    get = get or requests.get
    try:
        r = get(f"{nuvem_url}/api/espelho/snapshot", headers=_cabecalhos(token), timeout=300)
    except requests.RequestException as e:
        raise EspelhoErro(f"snapshot: sem resposta da nuvem: {e}") from e
    corpo = _resposta(r, "snapshot")
    itens = corpo.get("itens") or []
    # unidade/empresa vêm no próprio snapshot; o _pertence precisa do empresa_id
    uni = next((i["dados"] for i in itens if i["tabela"] == "unidades"), None)
    if not uni or int(uni["id"]) != int(unidade_id):
        raise EspelhoErro("snapshot não trouxe a unidade esperada")
    unidade = {"id": int(uni["id"]), "empresa_id": int(uni["empresa_id"])}
    aplicados, _ = aplicar_lote(unidade, itens, lado="agente")
    apos = 0
    while True:
        try:
            r = get(f"{nuvem_url}/api/espelho/fotos", headers=_cabecalhos(token), params={"apos": apos, "limite": 50}, timeout=120)
        except requests.RequestException as e:
            raise EspelhoErro(f"fotos: sem resposta da nuvem: {e}") from e
        pagina = _resposta(r, "fotos").get("itens") or []
        if not pagina:
            break
        aplicados += aplicar_lote(unidade, pagina, lado="agente")[0]
        apos = pagina[-1]["chave"]["pessoa_id"]
    cur = corpo.get("cursor") or {"xid": "0", "id": 0}
    _cursor(cur["xid"], cur["id"])
    db.execute("UPDATE espelho_estado SET carga_em = now() WHERE id = 1", outbox=False)
    return aplicados


def precisa_carga():
    est = estado_local()
    return est is None or est["carga_em"] is None


def marcar_recarga():
    db.execute("UPDATE espelho_estado SET carga_em = NULL WHERE id = 1", outbox=False)


def puxar(nuvem_url, token, unidade_id, get=None, post=None, limite=500, max_lotes=20):
    """Traz o que a nuvem mudou pra esta unidade desde o cursor. Devolve itens aplicados."""
    get, post = get or requests.get, post or requests.post
    unidade = _unidade_local(unidade_id)
    total = 0
    for _ in range(max_lotes):
        est = estado_local()
        try:
            r = get(f"{nuvem_url}/api/espelho/reverso", headers=_cabecalhos(token),
                    params={"desde_xid": est["cursor_xid"], "desde": est["cursor_nuvem"], "limite": limite}, timeout=90)
        except requests.RequestException as e:
            raise EspelhoErro(f"reverso: sem resposta da nuvem: {e}") from e
        corpo = _resposta(r, "reverso")
        itens, ate = corpo.get("itens") or [], corpo.get("ate")
        if itens:
            aplicar_lote(unidade, itens, lado="agente")
            total += len(itens)
        if not ate:
            break
        _cursor(ate["xid"], ate["id"])
        try:
            post(f"{nuvem_url}/api/espelho/reverso/confirmar", data=_json({"ate": ate}), headers=_cabecalhos(token), timeout=30)
        except requests.RequestException:
            pass  # confirmação é só limpeza; o cursor local já garante que não reaplica
        if not corpo.get("mais"):
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
            r = post(f"{nuvem_url}/api/espelho/receber", data=_json({"itens": itens}), headers=_cabecalhos(token), timeout=90)
        except requests.RequestException as e:
            raise EspelhoErro(f"sem resposta da nuvem: {e}") from e
        _resposta(r, "envio")
        confirmar_ids([i["id"] for i in itens])
        total += len(itens)
        if len(itens) < limite:
            break
    return total


def ping(nuvem_url, token, dados, post=None):
    """Sinal de vida. Devolve a resposta da nuvem (hora dela, versão do banco) ou None."""
    post = post or requests.post
    try:
        r = post(f"{nuvem_url}/api/espelho/ping", data=_json(dados), headers=_cabecalhos(token), timeout=15)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None
