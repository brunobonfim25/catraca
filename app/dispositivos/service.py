"""Dispositivos: fila de sincronização, mapeamento de leitores, status."""
import json

from .. import db

# [SUP] sem mapeamento cadastrado, assumimos o padrão de fábrica do idBlock Next:
# portal 1 / iDFace primário (0MS1) = entrada; portal 2 / secundário (0MS2) = saída.
SENTIDO_PADRAO_PORTAL = {1: "entrada", 2: "saida"}
SENTIDO_PADRAO_COMPONENT = {"0MS1": "entrada", "0MS2": "saida"}


def listar(unidade_id, apenas_ativos=True):
    sql = "SELECT * FROM dispositivos WHERE unidade_id = %s"
    if apenas_ativos:
        sql += " AND ativo"
    return db.query(sql + " ORDER BY nome", (unidade_id,))


def por_device_id(device_id_fabricante):
    return db.query("SELECT * FROM dispositivos WHERE device_id_fabricante = %s AND ativo", (int(device_id_fabricante),), one=True)


def sentido_do_leitor(dispositivo, component_id=None, portal_id=None):
    """Entrada ou saída, a partir do component_id (modo online) ou portal_id (access_log)."""
    rows = db.query("SELECT * FROM dispositivo_leitores WHERE dispositivo_id = %s", (dispositivo["id"],))
    if component_id:
        for r in rows:
            if r["component_id"] == component_id:
                return r["sentido"]
    if portal_id is not None:
        for r in rows:
            if r["portal_id"] == int(portal_id):
                return r["sentido"]
    if dispositivo["sentido"] in ("entrada", "saida"):
        return dispositivo["sentido"]
    if component_id and component_id in SENTIDO_PADRAO_COMPONENT:
        return SENTIDO_PADRAO_COMPONENT[component_id]
    if portal_id is not None and int(portal_id) in SENTIDO_PADRAO_PORTAL:
        return SENTIDO_PADRAO_PORTAL[int(portal_id)]
    return None


def criar_leitores_padrao(dispositivo_id):
    db.execute(
        "INSERT INTO dispositivo_leitores (dispositivo_id, component_id, portal_id, sentido, descricao) VALUES "
        "(%s,'0MS1',1,'entrada','iDFace primário (fora) — validar no hardware'),"
        "(%s,'0MS2',2,'saida','iDFace secundário (dentro) — validar no hardware')",
        (dispositivo_id, dispositivo_id),
    )


def atualizar_contato(dispositivo_id, status="online", **campos):
    sets = ["status = %s", "ultimo_contato = now()"]
    params = [status]
    for k, v in campos.items():
        if k in ("firmware", "serial", "device_id_fabricante", "ultimo_log_id"):
            sets.append(f"{k} = %s")
            params.append(v)
    params.append(dispositivo_id)
    db.execute(f"UPDATE dispositivos SET {', '.join(sets)} WHERE id = %s", params)


# ---------------------------------------------------------------- fila de sync

def enfileirar(dispositivo_id, tipo, pessoa_id=None, payload=None):
    """Um item por (dispositivo, pessoa, tipo) pendente: se já existe, não duplica."""
    existente = db.query(
        "SELECT id FROM fila_sync WHERE dispositivo_id = %s AND tipo = %s AND pessoa_id IS NOT DISTINCT FROM %s AND status = 'pendente'",
        (dispositivo_id, tipo, pessoa_id), one=True,
    )
    if existente:
        db.execute("UPDATE fila_sync SET payload = %s, proximo_em = now() WHERE id = %s", (json.dumps(payload or {}), existente["id"]))
        return existente["id"]
    return db.execute(
        "INSERT INTO fila_sync (dispositivo_id, pessoa_id, tipo, payload) VALUES (%s,%s,%s,%s) RETURNING id",
        (dispositivo_id, pessoa_id, tipo, json.dumps(payload or {})), returning=True,
    )["id"]


def enfileirar_pessoa(pessoa_id, unidade_id, com_foto=True, com_regras=True):
    """Cadastro/alteração de pessoa → sync_user (+ set_photo, set_rules) em cada catraca da unidade."""
    for d in listar(unidade_id):
        db.execute(
            "INSERT INTO dispositivo_pessoa (dispositivo_id, pessoa_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (d["id"], pessoa_id),
        )
        enfileirar(d["id"], "sync_user", pessoa_id)
        if com_foto:
            enfileirar(d["id"], "set_photo", pessoa_id)
        if com_regras:
            enfileirar(d["id"], "set_rules", pessoa_id)


def enfileirar_remocao(pessoa_id, unidade_id):
    for d in listar(unidade_id, apenas_ativos=False):
        enfileirar(d["id"], "remove_user", pessoa_id)


def enfileirar_todos_da_unidade(dispositivo_id, unidade_id):
    """Catraca nova (ou resetada): manda todo mundo ativo pra ela."""
    pessoas = db.query("SELECT id FROM pessoas WHERE unidade_id = %s AND status = 'ativo'", (unidade_id,))
    for p in pessoas:
        db.execute("INSERT INTO dispositivo_pessoa (dispositivo_id, pessoa_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (dispositivo_id, p["id"]))
        enfileirar(dispositivo_id, "sync_user", p["id"])
        enfileirar(dispositivo_id, "set_photo", p["id"])
        enfileirar(dispositivo_id, "set_rules", p["id"])
    return len(pessoas)


def enfileirar_regras(dispositivo_id=None, turma_id=None):
    """set_rules de quem já está na catraca (por catraca, ou dos alunos de uma turma).
    Revisão (C6): mudar "só sai com responsável" na turma não reenviava as regras; offline a
    catraca seguia com a regra antiga."""
    if turma_id is not None:
        rows = db.query("SELECT dp.dispositivo_id, dp.pessoa_id FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id "
                        "JOIN dispositivo_pessoa dp ON dp.pessoa_id = p.id JOIN dispositivos d ON d.id = dp.dispositivo_id "
                        "WHERE a.turma_id = %s AND p.status <> 'desligado' AND d.ativo", (turma_id,))
    else:
        rows = db.query("SELECT dp.dispositivo_id, dp.pessoa_id FROM dispositivo_pessoa dp JOIN pessoas p ON p.id = dp.pessoa_id "
                        "WHERE dp.dispositivo_id = %s AND p.status <> 'desligado'", (dispositivo_id,))
    for r in rows:
        enfileirar(r["dispositivo_id"], "set_rules", r["pessoa_id"])
    return len(rows)


def resumo_fila(unidade_id):
    return db.query(
        "SELECT f.status, count(*) AS n FROM fila_sync f JOIN dispositivos d ON d.id = f.dispositivo_id "
        "WHERE d.unidade_id = %s GROUP BY f.status",
        (unidade_id,),
    )


def status_sync_pessoa(pessoa_id):
    # Revisão (C10): o status dizia "sincronizado" com a remoção ou o bloqueio parados na fila
    return db.query(
        "SELECT dp.*, d.nome AS dispositivo_nome, d.status AS dispositivo_status, "
        "(SELECT string_agg(f.tipo || ': ' || f.status || COALESCE(' — ' || f.erro, ''), '; ' ORDER BY f.id) FROM fila_sync f "
        " WHERE f.dispositivo_id = dp.dispositivo_id AND f.pessoa_id = dp.pessoa_id AND f.status IN ('pendente','processando','erro')) AS fila_aberta "
        "FROM dispositivo_pessoa dp JOIN dispositivos d ON d.id = dp.dispositivo_id WHERE dp.pessoa_id = %s ORDER BY d.nome",
        (pessoa_id,),
    )
