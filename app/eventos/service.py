"""Ingestão de eventos: access_logs da catraca (Monitor ou coleta), decisões do modo Pro,
liberações manuais. Atualiza presença, espera de responsável e fila do Telegram.
"""
import json
import logging
from datetime import timedelta

from .. import db
from ..acesso import regras
from ..acesso import service as acesso
from ..dispositivos import service as dispositivos
from ..notificacoes import telegram
from ..util import agora_utc, de_unix, para_local, primeiro_nome
from . import tipos

log = logging.getLogger(__name__)

# Entrada de aluno até 60 s depois do responsável na mesma catraca = veio acompanhado
JANELA_ACOMPANHADO_SEG = 60


# ---------------------------------------------------------------- gravação

def registrar(unidade_id, tipo, ocorrido_em, dispositivo_id=None, pessoa_id=None, sentido=None, origem="dispositivo",
              log_id=None, evento_dispositivo=None, correlacao_id=None, autorizado_por=None, justificativa=None, detalhes=None):
    """Grava o evento. Devolve a linha, ou None se (dispositivo, log_id) já existia (dedupe)."""
    return db.execute(
        "INSERT INTO eventos (unidade_id, dispositivo_id, pessoa_id, tipo, sentido, ocorrido_em, origem, log_id_dispositivo, "
        "evento_dispositivo, correlacao_id, autorizado_por, justificativa, detalhes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (dispositivo_id, log_id_dispositivo) WHERE log_id_dispositivo IS NOT NULL DO NOTHING RETURNING *",
        (unidade_id, dispositivo_id, pessoa_id, tipo, sentido, ocorrido_em, origem, log_id, evento_dispositivo,
         correlacao_id, autorizado_por, justificativa, json.dumps(detalhes or {})),
        returning=True,
    )


def atualizar_presenca(pessoa_id, unidade_id, dentro, evento_id, quando):
    db.execute(
        "INSERT INTO presenca (pessoa_id, unidade_id, dentro, desde, ultimo_evento_id) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (pessoa_id) DO UPDATE SET dentro = EXCLUDED.dentro, desde = EXCLUDED.desde, ultimo_evento_id = EXCLUDED.ultimo_evento_id "
        # Coleta atrasada pode trazer evento mais velho que o estado atual: não volta no tempo
        "WHERE presenca.desde <= EXCLUDED.desde",
        (pessoa_id, unidade_id, dentro, quando, evento_id),
    )


def pessoa_por_user_id(dispositivo_id, user_id):
    if not user_id:
        return None
    return db.query(
        "SELECT p.* FROM dispositivo_pessoa dp JOIN pessoas p ON p.id = dp.pessoa_id "
        "WHERE dp.dispositivo_id = %s AND dp.user_id_no_dispositivo = %s",
        (dispositivo_id, int(user_id)), one=True,
    )


def _unidade(unidade_id):
    return db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)


def _turma_do_aluno(pessoa_id):
    return db.query(
        "SELECT t.*, a.id AS aluno_id FROM alunos a JOIN turmas t ON t.id = a.turma_id WHERE a.pessoa_id = %s",
        (pessoa_id,), one=True,
    )


# ---------------------------------------------------------------- responsável em espera

def registrar_espera(unidade, responsavel_pessoa, dispositivo=None, quando=None, log_id=None, evento_dispositivo=None):
    """Responsável se identificou no leitor de fora → fica em espera por N minutos."""
    quando = quando or agora_utc()
    resp = db.query("SELECT * FROM responsaveis WHERE pessoa_id = %s", (responsavel_pessoa["id"],), one=True)
    minutos = int((unidade.get("config") or {}).get("espera_responsavel_min") or 15)
    expira = quando + timedelta(minutes=minutos)
    espera = db.execute(
        "INSERT INTO responsavel_em_espera (unidade_id, responsavel_id, identificado_em, expira_em, dispositivo_id) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (unidade["id"], resp["id"], quando, expira, dispositivo["id"] if dispositivo else None), returning=True,
    )
    alunos = db.query(
        "SELECT p.nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas p ON p.id = a.pessoa_id WHERE ar.responsavel_id = %s",
        (resp["id"],),
    )
    evento = registrar(
        unidade["id"], tipos.RESPONSAVEL_CHEGOU, quando, dispositivo_id=dispositivo["id"] if dispositivo else None,
        pessoa_id=responsavel_pessoa["id"], sentido="entrada", log_id=log_id, evento_dispositivo=evento_dispositivo,
        detalhes={"espera_id": espera["id"], "alunos": [a["nome"] for a in alunos]},
    )
    return espera, evento


def consumir_espera(espera_id, aluno_id):
    db.execute("UPDATE responsavel_em_espera SET consumido_em = now(), aluno_id = %s WHERE id = %s", (aluno_id, espera_id))


# ---------------------------------------------------------------- Telegram

def texto_evento(tipo, pessoa, turma_nome, quando_local, retirado_por=None, parentesco=None, justificativa=None):
    nome = primeiro_nome(pessoa["nome"])
    turma = f" ({turma_nome})" if turma_nome else ""
    hora = quando_local.strftime("%H:%M")
    if tipo == tipos.ENTRADA:
        return f"{nome}{turma} entrou às {hora}."
    if tipo == tipos.ENTRADA_ATRASADA:
        return f"{nome}{turma} entrou às {hora} (após o horário de tolerância)."
    if tipo == tipos.SAIDA_ALUNO:
        quem = primeiro_nome(retirado_por) + (f" ({parentesco})" if parentesco else "")
        return f"{nome} saiu às {hora} com {quem}."
    if tipo == tipos.SAIDA_AUTONOMA:
        return f"{nome}{turma} saiu sozinho(a) às {hora}."
    if tipo == tipos.SAIDA_ANTECIPADA:
        j = f" Motivo: {justificativa}" if justificativa else ""
        return f"{nome}{turma} teve saída antecipada liberada pela escola às {hora}.{j}"
    if tipo == tipos.SAIDA_NEGADA:
        return f"{nome} tentou sair às {hora} e a catraca não liberou (sem responsável autorizado). A recepção foi avisada."
    if tipo == tipos.SAIDA:
        return f"{nome}{turma} saiu às {hora}."
    return None


def notificar_responsaveis(aluno_pessoa, texto, evento_id, unidade_id):
    if not texto:
        return 0
    resps = db.query(
        "SELECT r.id FROM aluno_responsavel ar JOIN responsaveis r ON r.id = ar.responsavel_id "
        "JOIN alunos a ON a.id = ar.aluno_id WHERE a.pessoa_id = %s",
        (aluno_pessoa["id"],),
    )
    for r in resps:
        telegram.enfileirar(unidade_id, r["id"], texto, evento_id)
    return len(resps)


# ---------------------------------------------------------------- access_logs (Standalone / coleta / espelho do Pro)

TIPO_POR_CODIGO = {
    3: tipos.NAO_IDENTIFICADO, 5: tipos.NAO_IDENTIFICADO, 10: tipos.LIBERACAO_MANUAL,
    11: tipos.BOTOEIRA, 12: tipos.LIBERACAO_MANUAL, 13: tipos.DESISTENCIA,
}


def _evento_pro_sem_log(dispositivo_id, pessoa_id, ocorrido_em, codigo):
    """No modo Pro o servidor já gravou o evento ao decidir; quando o access_log chega pelo
    Monitor, só anexa o log_id em vez de duplicar."""
    return db.query(
        "SELECT id FROM eventos WHERE dispositivo_id = %s AND pessoa_id IS NOT DISTINCT FROM %s AND log_id_dispositivo IS NULL "
        "AND evento_dispositivo = %s AND ocorrido_em BETWEEN %s AND %s ORDER BY id DESC LIMIT 1",
        (dispositivo_id, pessoa_id, codigo, ocorrido_em - timedelta(seconds=10), ocorrido_em + timedelta(seconds=10)), one=True,
    )


def processar_access_log(dispositivo, item):
    """item = linha de access_logs da Control iD (id, time, event, user_id, portal_id…).
    Idempotente: o mesmo log pelo Monitor e pela coleta vira um evento só."""
    log_id = int(item["id"])
    ja = db.query("SELECT id FROM eventos WHERE dispositivo_id = %s AND log_id_dispositivo = %s", (dispositivo["id"], log_id), one=True)
    if ja:
        return None
    codigo = int(item.get("event") or 0)
    user_id = int(item.get("user_id") or 0)
    ocorrido = de_unix(item["time"]) if item.get("time") else agora_utc()
    unidade = _unidade(dispositivo["unidade_id"])
    sentido = dispositivos.sentido_do_leitor(dispositivo, portal_id=item.get("portal_id"))
    pessoa = pessoa_por_user_id(dispositivo["id"], user_id)

    pendente = _evento_pro_sem_log(dispositivo["id"], pessoa["id"] if pessoa else None, ocorrido, codigo) if codigo in (6, 7) else None
    if pendente:
        db.execute("UPDATE eventos SET log_id_dispositivo = %s WHERE id = %s", (log_id, pendente["id"]))
        _avancar_ultimo_log(dispositivo, log_id)
        return db.query("SELECT * FROM eventos WHERE id = %s", (pendente["id"],), one=True)

    base = dict(dispositivo_id=dispositivo["id"], pessoa_id=pessoa["id"] if pessoa else None, sentido=sentido,
                log_id=log_id, evento_dispositivo=codigo, detalhes={"user_id": user_id, "portal_id": item.get("portal_id")})
    evento = None
    if codigo == 7:
        evento = _processar_liberado(unidade, dispositivo, pessoa, sentido, ocorrido, base)
    elif codigo == 6:
        tipo = tipos.SAIDA_NEGADA if sentido == "saida" else tipos.ACESSO_NEGADO
        evento = registrar(unidade["id"], tipo, ocorrido, **base)
        if evento and pessoa and pessoa["tipo"] == "aluno" and sentido == "saida":
            turma = _turma_do_aluno(pessoa["id"])
            notificar_responsaveis(pessoa, texto_evento(tipo, pessoa, turma and turma["nome"], para_local(ocorrido, unidade["fuso"])), evento["id"], unidade["id"])
    elif codigo == 13:
        evento = registrar(unidade["id"], tipos.DESISTENCIA, ocorrido, **base)
        # Liberou e não girou: desfaz a presença que o evento 7 tinha marcado
        if evento and pessoa and sentido:
            atualizar_presenca(pessoa["id"], unidade["id"], sentido != "entrada", evento["id"], ocorrido)
    else:
        tipo = TIPO_POR_CODIGO.get(codigo, f"DISPOSITIVO_EVENTO_{codigo}")
        evento = registrar(unidade["id"], tipo, ocorrido, **base)
    _avancar_ultimo_log(dispositivo, log_id)
    return evento


def _avancar_ultimo_log(dispositivo, log_id):
    db.execute("UPDATE dispositivos SET ultimo_log_id = GREATEST(ultimo_log_id, %s) WHERE id = %s", (log_id, dispositivo["id"]))


def _processar_liberado(unidade, dispositivo, pessoa, sentido, ocorrido, base):
    """Evento 7 (catraca liberou) em Standalone: classifica e atualiza presença."""
    local = para_local(ocorrido, unidade["fuso"])
    if pessoa is None:
        return registrar(unidade["id"], tipos.ENTRADA if sentido == "entrada" else tipos.SAIDA, ocorrido, **base)

    if pessoa["tipo"] == "responsavel":
        if sentido == "entrada":
            _, evento = registrar_espera(unidade, pessoa, dispositivo, ocorrido, log_id=base["log_id"], evento_dispositivo=7)
            return evento
        return registrar(unidade["id"], tipos.SAIDA, ocorrido, **base)

    turma = _turma_do_aluno(pessoa["id"]) if pessoa["tipo"] == "aluno" else None
    if sentido == "entrada":
        tipo = regras.classificar_entrada(turma, local) if turma else tipos.ENTRADA
        acompanhado = _responsavel_acompanhando(unidade["id"], dispositivo["id"], pessoa, ocorrido) if turma else None
        if acompanhado:
            base["correlacao_id"] = acompanhado["pessoa_id"]
            base["detalhes"]["acompanhado_por"] = acompanhado["nome"]
        evento = registrar(unidade["id"], tipo, ocorrido, **base)
        if evento:
            atualizar_presenca(pessoa["id"], unidade["id"], True, evento["id"], ocorrido)
            if turma:
                notificar_responsaveis(pessoa, texto_evento(tipo, pessoa, turma["nome"], local), evento["id"], unidade["id"])
        return evento

    # saída liberada pela catraca (Standalone): registra o que o servidor teria decidido
    tipo, correlacao, detalhes_extra, parentesco, retirado_por = tipos.SAIDA, None, {}, None, None
    if turma:
        decisao = acesso.decidir(pessoa["id"], "saida", unidade, ocorrido)
        if decisao.liberar:
            tipo = decisao.tipo
            if decisao.espera_id:
                consumir_espera(decisao.espera_id, turma["aluno_id"])
                resp = db.query("SELECT p.id, p.nome, ar.parentesco FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                                "LEFT JOIN aluno_responsavel ar ON ar.responsavel_id = r.id AND ar.aluno_id = %s WHERE r.id = %s",
                                (turma["aluno_id"], decisao.responsavel_id), one=True)
                correlacao, retirado_por, parentesco = resp["id"], resp["nome"], resp["parentesco"]
        else:
            # A catraca liberou sem o servidor concordar (Standalone na saída): fica visível no painel
            detalhes_extra = {"alerta": f"saída liberada pela catraca: {decisao.motivo}"}
    base["correlacao_id"] = correlacao
    base["detalhes"].update(detalhes_extra)
    evento = registrar(unidade["id"], tipo, ocorrido, **base)
    if evento:
        atualizar_presenca(pessoa["id"], unidade["id"], False, evento["id"], ocorrido)
        if turma:
            notificar_responsaveis(pessoa, texto_evento(tipo, pessoa, turma["nome"], local, retirado_por, parentesco), evento["id"], unidade["id"])
    return evento


def _responsavel_acompanhando(unidade_id, dispositivo_id, aluno_pessoa, ocorrido):
    """Responsável do aluno que se identificou na mesma catraca nos 60 s anteriores."""
    return db.query(
        "SELECT p.id AS pessoa_id, p.nome FROM eventos e JOIN pessoas p ON p.id = e.pessoa_id "
        "JOIN responsaveis r ON r.pessoa_id = p.id JOIN aluno_responsavel ar ON ar.responsavel_id = r.id "
        "JOIN alunos a ON a.id = ar.aluno_id WHERE a.pessoa_id = %s AND e.dispositivo_id = %s AND e.unidade_id = %s "
        "AND e.tipo = %s AND e.ocorrido_em BETWEEN %s AND %s ORDER BY e.ocorrido_em DESC LIMIT 1",
        (aluno_pessoa["id"], dispositivo_id, unidade_id, tipos.RESPONSAVEL_CHEGOU, ocorrido - timedelta(seconds=JANELA_ACOMPANHADO_SEG), ocorrido), one=True,
    )


# ---------------------------------------------------------------- modo Pro (decisão online)

def decidir_online(dispositivo, campos):
    """Catraca em modo Pro identificou alguém e pergunta se libera.
    Devolve (resposta_para_catraca, evento). Formato [DOC]: {"result": {event, user_id, user_name, actions[], message}}."""
    unidade = _unidade(dispositivo["unidade_id"])
    user_id = int(campos.get("user_id") or 0)
    ocorrido = de_unix(campos["time"]) if campos.get("time") else agora_utc()
    sentido = dispositivos.sentido_do_leitor(dispositivo, component_id=campos.get("component_id"), portal_id=campos.get("portal_id"))
    pessoa = pessoa_por_user_id(dispositivo["id"], user_id)
    cfg = dispositivo.get("config") or {}
    allow = {"entrada": cfg.get("allow_entrada", "clockwise"), "saida": cfg.get("allow_saida", "anticlockwise")}.get(sentido, "both")
    detalhes = {"user_id": user_id, "component_id": campos.get("component_id"), "portal_id": campos.get("portal_id"), "modo": "pro"}

    def negar(tipo, motivo, msg):
        ev = registrar(unidade["id"], tipo, ocorrido, dispositivo_id=dispositivo["id"], pessoa_id=pessoa["id"] if pessoa else None,
                       sentido=sentido, evento_dispositivo=6, detalhes={**detalhes, "motivo": motivo})
        return {"result": {"event": 6, "user_id": user_id, "user_name": pessoa["nome"] if pessoa else "", "message": msg[:60]}}, ev

    if pessoa is None:
        return negar(tipos.ACESSO_NEGADO, "user_id sem pessoa", "Não cadastrado")

    if pessoa["tipo"] == "responsavel" and sentido == "entrada":
        espera, ev = registrar_espera(unidade, pessoa, dispositivo, ocorrido, evento_dispositivo=7)
        alunos = ", ".join(primeiro_nome(a) for a in (ev["detalhes"].get("alunos") or [])) or "aluno"
        modo = (unidade.get("config") or {}).get("responsavel_resposta", "aguardar")
        # [SUP] event 7 sem actions = tela verde sem girar; ver README do adaptador, item 6
        resultado = {"event": 7, "user_id": user_id, "user_name": pessoa["nome"], "message": f"Aguarde {alunos}"[:60]}
        if modo == "liberar":
            resultado["actions"] = [{"action": "catra", "parameters": f"allow={allow}"}]
        elif modo == "negar":
            resultado["event"] = 6
        return {"result": resultado}, ev

    decisao = acesso.decidir(pessoa["id"], sentido, unidade, ocorrido)
    if not decisao.liberar:
        msg = {"saida": "Aguardando responsável", "entrada": "Acesso negado"}.get(sentido, "Negado")
        resp, ev = negar(decisao.tipo, decisao.motivo, msg)
        if pessoa["tipo"] == "aluno" and sentido == "saida" and ev:
            turma = _turma_do_aluno(pessoa["id"])
            notificar_responsaveis(pessoa, texto_evento(tipos.SAIDA_NEGADA, pessoa, turma and turma["nome"], para_local(ocorrido, unidade["fuso"])), ev["id"], unidade["id"])
        return resp, ev

    correlacao, retirado_por, parentesco = None, None, None
    turma = _turma_do_aluno(pessoa["id"]) if pessoa["tipo"] == "aluno" else None
    if decisao.espera_id and turma:
        consumir_espera(decisao.espera_id, turma["aluno_id"])
        r = db.query("SELECT p.id, p.nome, ar.parentesco FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                     "LEFT JOIN aluno_responsavel ar ON ar.responsavel_id = r.id AND ar.aluno_id = %s WHERE r.id = %s",
                     (turma["aluno_id"], decisao.responsavel_id), one=True)
        correlacao, retirado_por, parentesco = r["id"], r["nome"], r["parentesco"]
    ev = registrar(unidade["id"], decisao.tipo, ocorrido, dispositivo_id=dispositivo["id"], pessoa_id=pessoa["id"], sentido=sentido,
                   evento_dispositivo=7, correlacao_id=correlacao, detalhes={**detalhes, "motivo": decisao.motivo})
    if ev:
        atualizar_presenca(pessoa["id"], unidade["id"], sentido == "entrada", ev["id"], ocorrido)
        if turma:
            notificar_responsaveis(pessoa, texto_evento(decisao.tipo, pessoa, turma["nome"], para_local(ocorrido, unidade["fuso"]), retirado_por, parentesco), ev["id"], unidade["id"])
    mensagem = {"entrada": "Bem-vindo(a)", "saida": "Até amanhã"}.get(sentido, "Liberado")
    return {"result": {"event": 7, "user_id": user_id, "user_name": pessoa["nome"], "message": mensagem,
                       "actions": [{"action": "catra", "parameters": f"allow={allow}"}]}}, ev


# ---------------------------------------------------------------- liberação manual

def liberar_manual(unidade, usuario_id, sentido, justificativa, pessoa_id=None, dispositivo_id=None, nome_visitante=None, girar_catraca=True):
    """Recepção libera com justificativa. Fica no log com quem autorizou; pais recebem aviso."""
    quando = agora_utc()
    pessoa = db.query("SELECT * FROM pessoas WHERE id = %s", (pessoa_id,), one=True) if pessoa_id else None
    tipo = tipos.LIBERACAO_MANUAL
    turma = None
    if pessoa and pessoa["tipo"] == "aluno":
        turma = _turma_do_aluno(pessoa["id"])
        tipo = tipos.SAIDA_ANTECIPADA if sentido == "saida" else tipos.ENTRADA
    detalhes = {"manual": True}
    if nome_visitante:
        detalhes["visitante"] = nome_visitante
    evento = registrar(unidade["id"], tipo, quando, dispositivo_id=dispositivo_id, pessoa_id=pessoa_id, sentido=sentido, origem="manual",
                       autorizado_por=usuario_id, justificativa=justificativa, detalhes=detalhes)
    if pessoa:
        atualizar_presenca(pessoa["id"], unidade["id"], sentido == "entrada", evento["id"], quando)
        if turma:
            notificar_responsaveis(pessoa, texto_evento(tipo, pessoa, turma["nome"], para_local(quando, unidade["fuso"]), justificativa=justificativa), evento["id"], unidade["id"])
    erro = None
    if girar_catraca and dispositivo_id:
        erro = _liberar_na_catraca(dispositivo_id, sentido)
        if erro:
            db.execute("UPDATE eventos SET detalhes = detalhes || %s WHERE id = %s", (json.dumps({"erro_catraca": erro}), evento["id"]))
    return evento, erro


def _liberar_na_catraca(dispositivo_id, sentido):
    """Quem gira a catraca é o worker (na escola): entra na fila como comando 'liberar'."""
    d = db.query("SELECT id FROM dispositivos WHERE id = %s AND ativo", (dispositivo_id,), one=True)
    if not d:
        return "dispositivo não encontrado"
    db.execute(
        "INSERT INTO fila_sync (dispositivo_id, tipo, payload) VALUES (%s, 'liberar', %s)",
        (dispositivo_id, json.dumps({"sentido": {"entrada": "entrada", "saida": "saida"}.get(sentido, "ambos")})),
    )
    return None


# ---------------------------------------------------------------- Monitor: outros endpoints

def processar_catra_event(dispositivo, payload):
    """[DOC] confirmação de giro (TURN_LEFT/TURN_RIGHT) ou desistência (GIVE_UP) de um access_event_id."""
    ev = payload.get("event") or {}
    aid = payload.get("access_event_id")
    if aid:
        db.execute("UPDATE eventos SET detalhes = detalhes || %s WHERE dispositivo_id = %s AND log_id_dispositivo = %s",
                   (json.dumps({"giro": ev.get("name")}), dispositivo["id"], int(aid)))


def processar_operation_mode(dispositivo, payload):
    om = payload.get("operation_mode") or {}
    nome = str(om.get("mode_name") or "").lower()
    contingencia = "conting" in nome
    tipo = tipos.DISPOSITIVO_CONTINGENCIA if contingencia else tipos.DISPOSITIVO_ONLINE
    registrar(dispositivo["unidade_id"], tipo, agora_utc(), dispositivo_id=dispositivo["id"], origem="sistema", detalhes=om)
    # Fica vermelho no painel até a catraca avisar que voltou ao modo online
    db.execute("UPDATE dispositivos SET status = %s, ultimo_contato = now() WHERE id = %s", ("contingencia" if contingencia else "online", dispositivo["id"]))
