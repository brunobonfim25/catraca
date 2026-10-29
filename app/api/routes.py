"""Endpoints que a catraca chama (Monitor e modo Pro) + JSON do painel (polling).

Autenticação da catraca: token da escola na URL (unidades.config.monitor_token). A Control iD
não assina as notificações; o token é segredo compartilhado configurado em monitor.path e na
URL do servidor online. Tudo que chega aqui é dado externo: validado e logado, nunca confiado.
"""
import hmac
import logging
import os

from flask import Blueprint, abort, current_app, g, jsonify, request

from .. import db
from ..auth.rbac import login_obrigatorio, unidade_atual_ou_403
from ..dispositivos import service as dispositivos
from ..eventos import service as eventos
from ..eventos import tipos
from ..util import de_unix, formatar_hora

bp = Blueprint("api", __name__, url_prefix="/api")
log = logging.getLogger(__name__)


def _checar_token(token):
    """Token da URL → escola. Revisão (S7): era um token só pra todas as escolas; quem tinha o
    .env de uma escola mandava "evento da catraca" de qualquer outra para a nuvem. Agora cada
    escola tem o seu (unidades.config.monitor_token) e a catraca precisa ser daquela escola.
    O MONITOR_TOKEN do .env só vale fora da nuvem (escola única ou o próprio agente)."""
    from ..espelho.service import unidade_por_monitor_token

    token = token or ""
    un = unidade_por_monitor_token(token)
    if un and hmac.compare_digest((un.get("config") or {}).get("monitor_token", ""), token):
        g.unidade_token_id = un["id"]
        return
    esperado = current_app.config.get("MONITOR_TOKEN") or ""
    if current_app.config.get("MODO") != "nuvem" and esperado and hmac.compare_digest(esperado, token):
        uid = current_app.config.get("UNIDADE_ID") if current_app.config.get("MODO") == "local" else None
        g.unidade_token_id = int(uid) if uid else None
        return
    abort(403)


def _da_escola_do_token(d):
    esperado = getattr(g, "unidade_token_id", None)
    if d and esperado is not None and d["unidade_id"] != esperado:
        log.warning("catraca %s (escola %s) chamou com o token da escola %s", d["id"], d["unidade_id"], esperado)
        abort(403)
    return d


def _dispositivo_do_payload(device_id):
    if device_id in (None, ""):
        abort(400, "device_id ausente")
    d = dispositivos.por_device_id(device_id)
    if not d:
        log.warning("notificação de device_id desconhecido: %s", device_id)
        abort(404, "dispositivo não cadastrado")
    _da_escola_do_token(d)
    g.unidade_api_id = d["unidade_id"]
    return d


# ---------------------------------------------------------------- Monitor (catraca → servidor)

@bp.post("/notifications/<token>/<endpoint>")
def monitor(token, endpoint):
    _checar_token(token)
    payload = request.get_json(silent=True) or {}
    d = _dispositivo_do_payload(payload.get("device_id"))
    # contingência só sai pelo operation_mode (tratado abaixo); outras notificações não limpam
    dispositivos.atualizar_contato(d["id"], "contingencia" if d["status"] == "contingencia" else "online")
    if endpoint == "dao":
        n = 0
        for mudanca in payload.get("object_changes") or []:
            if mudanca.get("object") == "access_logs" and mudanca.get("type") == "inserted":
                valores = mudanca.get("values") or {}
                valores.setdefault("device_id", payload.get("device_id"))
                if eventos.processar_access_log(d, valores):
                    n += 1
        return jsonify({"ok": True, "eventos": n})
    if endpoint == "catra_event":
        eventos.processar_catra_event(d, payload)
    elif endpoint == "operation_mode":
        eventos.processar_operation_mode(d, payload)
    elif endpoint == "device_is_alive":
        pendentes = int(payload.get("access_logs") or 0)
        db.execute("UPDATE dispositivos SET config = config || %s WHERE id = %s", ('{"logs_no_dispositivo": %d}' % pendentes, d["id"]))
    # door, secbox, usb_drive, access_photo…: só registramos o contato
    return jsonify({"ok": True})


# ---------------------------------------------------------------- Modo Pro (servidor online)

@bp.route("/catraca/<token>/session_is_valid.fcgi", methods=["POST", "GET"])
def online_session_is_valid(token):
    # [DOC] a catraca checa o servidor online por este endpoint
    _checar_token(token)
    return jsonify({"session_is_valid": True})


@bp.post("/catraca/<token>/new_user_identified.fcgi")
def online_new_user_identified(token):
    # [DOC] form-urlencoded: device_id, identifier_id, time, portal_id, uuid, user_id, user_name, event, component_id…
    _checar_token(token)
    campos = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    d = _dispositivo_do_payload(campos.get("device_id"))
    dispositivos.atualizar_contato(d["id"], "online")
    resposta, _ = eventos.decidir_online(d, campos)
    return jsonify(resposta)


@bp.post("/catraca/<token>/device_is_alive.fcgi")
def online_device_is_alive(token):
    # [DOC] em contingência a catraca manda isso a cada minuto; HTTP OK = volta ao modo online
    _checar_token(token)
    payload = request.get_json(silent=True) or {}
    d = _da_escola_do_token(dispositivos.por_device_id(payload.get("device_id")) if payload.get("device_id") else None)
    if d:
        g.unidade_api_id = d["unidade_id"]
        dispositivos.atualizar_contato(d["id"], "online")
    return ""


@bp.post("/catraca/<token>/new_rex_log.fcgi")
def online_rex(token):
    # [DOC] botoeira em modo online: JSON {device_id, rex_log: {time, event: 11, user_id, portal_id}},
    # resposta vazia. O mesmo acionamento também chega como access_log 11 (Monitor/coleta): os dois
    # viram um evento só (registrar_botoeira_online), por isso a hora tem que ser a da catraca.
    _checar_token(token)
    payload = request.get_json(silent=True) or {}
    d = _da_escola_do_token(dispositivos.por_device_id(payload.get("device_id")) if payload.get("device_id") else None)
    if d:
        g.unidade_api_id = d["unidade_id"]
        rex = payload.get("rex_log") if isinstance(payload.get("rex_log"), dict) else {}
        quando = de_unix(rex["time"]) if rex.get("time") else eventos.agora_utc()
        sentido = dispositivos.sentido_do_leitor(d, portal_id=rex.get("portal_id")) if rex.get("portal_id") else None
        eventos.registrar_botoeira_online(d, quando, sentido, payload)
    return ""


@bp.route("/catraca/<token>/<path:outro>", methods=["POST"])
def online_outros(token, outro):
    # Enterprise (new_card, new_biometric_*…) não é usado: responde negado pra não travar a catraca
    _checar_token(token)
    return jsonify({"result": {"event": 6, "message": "Modo não suportado"}})


# ---------------------------------------------------------------- Railway (deploy falhou/caiu)

@bp.post("/railway/webhook")
def railway_webhook():
    """Railway avisa quando um deploy falha ou um serviço cai (o worker caído não consegue
    avisar sozinho). Token na URL porque o webhook do Railway não manda cabeçalho próprio."""
    from .. import alertas

    esperado = os.environ.get("RAILWAY_WEBHOOK_TOKEN", "")
    if not esperado or not hmac.compare_digest(esperado, request.args.get("token", "")):
        abort(403)
    payload = request.get_json(silent=True) or {}
    log.info("railway webhook: %s", str(payload)[:500])
    resultado = alertas.railway_evento(payload)
    if resultado == "aberto":
        alertas.verificar()  # avisa já, sem esperar a próxima volta do vigia
    return jsonify({"ok": True, "resultado": resultado})


# ---------------------------------------------------------------- Painel (polling 5 s)

@bp.get("/painel/estado")
@login_obrigatorio
def painel_estado():
    un = unidade_atual_ou_403()
    fuso = un["fuso"]
    presentes = db.query(
        "SELECT t.nome AS turma, count(*) AS n FROM presenca pr JOIN alunos a ON a.pessoa_id = pr.pessoa_id JOIN turmas t ON t.id = a.turma_id "
        "WHERE pr.unidade_id = %s AND pr.dentro GROUP BY t.nome ORDER BY t.nome", (un["id"],),
    )
    esperas = db.query(
        "SELECT e.id, p.nome AS responsavel, e.identificado_em, e.expira_em, "
        "(SELECT string_agg(pa.nome, ', ') FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas pa ON pa.id = a.pessoa_id WHERE ar.responsavel_id = e.responsavel_id) AS alunos "
        "FROM responsavel_em_espera e JOIN responsaveis r ON r.id = e.responsavel_id JOIN pessoas p ON p.id = r.pessoa_id "
        "WHERE e.unidade_id = %s AND e.consumido_em IS NULL AND e.expira_em > now() ORDER BY e.identificado_em DESC", (un["id"],),
    )
    ultimos = db.query(
        "SELECT e.id, e.tipo, e.sentido, e.ocorrido_em, p.nome AS pessoa, t.nome AS turma, d.nome AS dispositivo, e.detalhes, e.justificativa, pc.nome AS correlacao "
        "FROM eventos e LEFT JOIN pessoas p ON p.id = e.pessoa_id LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id "
        "LEFT JOIN dispositivos d ON d.id = e.dispositivo_id LEFT JOIN pessoas pc ON pc.id = e.correlacao_id "
        "WHERE e.unidade_id = %s ORDER BY e.ocorrido_em DESC, e.id DESC LIMIT 30", (un["id"],),
    )
    disps = db.query("SELECT id, nome, status, modo, ultimo_contato FROM dispositivos WHERE unidade_id = %s AND ativo ORDER BY nome", (un["id"],))
    fila = db.query("SELECT f.status, count(*) AS n FROM fila_sync f JOIN dispositivos d ON d.id = f.dispositivo_id WHERE d.unidade_id = %s GROUP BY f.status", (un["id"],))
    return jsonify({
        "presentes": [dict(r) for r in presentes],
        "total_dentro": sum(r["n"] for r in presentes),
        "esperas": [{**dict(r), "identificado_em": formatar_hora(r["identificado_em"], fuso), "expira_em": formatar_hora(r["expira_em"], fuso)} for r in esperas],
        "ultimos": [{**dict(r), "hora": formatar_hora(r["ocorrido_em"], fuso), "ocorrido_em": r["ocorrido_em"].isoformat(), "rotulo": tipos.ROTULOS.get(r["tipo"], r["tipo"]), "negado": r["tipo"] in tipos.NEGADOS} for r in ultimos],
        "dispositivos": [{**dict(r), "ultimo_contato": formatar_hora(r["ultimo_contato"], fuso) if r["ultimo_contato"] else None} for r in disps],
        "fila": {r["status"]: r["n"] for r in fila},
    })
