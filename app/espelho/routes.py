"""Endpoints da sincronização (servidos pela NUVEM) e a página de status do AGENTE."""
import hmac
import json
import logging
import os

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request

from .. import db
from ..auditoria import auditar
from ..auth.rbac import login_obrigatorio
from . import service

bp = Blueprint("espelho", __name__)
log = logging.getLogger(__name__)


def _unidade_do_agente():
    """Escola do agente pelo cabeçalho X-Espelho-Token (nunca pela URL: vai pra log de acesso)."""
    if current_app.config.get("MODO") != "nuvem":
        abort(404)
    token = request.headers.get("X-Espelho-Token", "")
    unidade = service.unidade_por_token(token) if token else None
    if not unidade or not hmac.compare_digest((unidade.get("config") or {}).get("espelho_token", ""), token):
        abort(403)
    try:
        service.conferir_agente(unidade, request.headers.get("X-Agente-Id"))
    except service.OutroAgente as e:
        abort(Response(json.dumps({"ok": False, "erro": str(e)}), status=409, content_type="application/json"))
    return unidade


@bp.post("/api/espelho/receber")
def receber():
    """Agente → nuvem: eventos, presença, status."""
    unidade = _unidade_do_agente()
    itens = (request.get_json(silent=True) or {}).get("itens") or []
    try:
        aplicados, rejeitados = service.receber_do_agente(unidade, itens)
    except Exception as e:  # noqa: BLE001 — o agente reenvia; o que importa é registrar o motivo
        log.exception("espelho da unidade %s falhou", unidade["id"])
        service._marcar(unidade["id"], {"agente_erro": str(e)[:300]})
        return jsonify({"ok": False, "erro": str(e)[:300]}), 500
    return jsonify({"ok": True, "aplicados": aplicados, "rejeitados": rejeitados})


@bp.get("/api/espelho/snapshot")
def snapshot():
    unidade = _unidade_do_agente()
    return jsonify({"ok": True, **service.snapshot(unidade)})


@bp.get("/api/espelho/fotos")
def fotos():
    unidade = _unidade_do_agente()
    return jsonify({"ok": True, "itens": service.fotos(unidade, request.args.get("apos", 0, type=int), request.args.get("limite", 50, type=int))})


@bp.get("/api/espelho/reverso")
def reverso():
    """Nuvem → agente: o que mudou pra esta unidade depois do cursor (xid, id)."""
    unidade = _unidade_do_agente()
    limite = max(1, min(request.args.get("limite", 500, type=int), 1000))
    try:
        lote = service.lote_para_agente(unidade, request.args.get("desde_xid", "0"), request.args.get("desde", 0, type=int), limite)
    except service.RecargaNecessaria as e:
        return jsonify({"ok": False, "recarga": True, "erro": str(e)}), 409
    return jsonify({"ok": True, **lote})


@bp.post("/api/espelho/reverso/confirmar")
def reverso_confirmar():
    unidade = _unidade_do_agente()
    ate = (request.get_json(silent=True) or {}).get("ate") or {}
    if isinstance(ate, dict) and ate.get("xid") is not None and ate.get("id") is not None:
        service.confirmar_ate(unidade["id"], ate["xid"], ate["id"])
    return jsonify({"ok": True})


@bp.post("/api/espelho/ping")
def ping():
    unidade = _unidade_do_agente()
    return jsonify({"ok": True, **service.registrar_ping(unidade, request.get_json(silent=True) or {})})


@bp.get("/api/espelho/status")
@login_obrigatorio
def status():
    """Situação dos servidores de escola: só das escolas que o usuário pode ver (antes listava
    IP e erros de todas as escolas de todos os clientes para qualquer usuário logado)."""
    from flask import g

    from ..auth.rbac import tem_permissao, unidades_visiveis

    if not tem_permissao(g.usuario, "configurar_escola"):
        return jsonify([]), 403
    ids = [u["id"] for u in unidades_visiveis(g.usuario)]
    rows = db.query("SELECT id, nome, config->>'agente_visto_em' AS visto_em, config->>'agente_erro' AS erro, config->'agente' AS agente "
                    "FROM unidades WHERE id = ANY(%s) ORDER BY nome", (ids,))
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- instalador do agente (servido pela nuvem)

_RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INSTALADOR = os.path.join(_RAIZ, "deploy", "windows", "instalador.ps1")
_MARCA_PS1 = "#__NEXOSIX_PS1__#"


def instalador_ps1(unidade, nuvem_url):
    """PowerShell com os dados da unidade embutidos (o token do agente vai dentro)."""
    with open(_INSTALADOR, encoding="utf-8") as f:
        corpo = f.read()
    return (corpo.replace("__NUVEM_URL__", nuvem_url).replace("__TOKEN__", (unidade.get("config") or {}).get("espelho_token", ""))
            .replace("__UNIDADE_ID__", str(unidade["id"])).replace("__UNIDADE_NOME__", unidade["nome"].replace('"', "").replace("`", "")))


@bp.get("/unidades/<int:unidade_id>/instalador.cmd")
@login_obrigatorio
def instalador_cmd(unidade_id):
    """Arquivo de dois cliques com o instalador inteiro dentro: pede elevação, extrai o .ps1
    numa pasta só de administradores, roda e apaga.

    Revisão (E18/S17): antes o .cmd baixava o .ps1 de /instalador/<token>.ps1 — link público
    com o token na URL (qualquer um com o link instalava um "agente" da escola, e o token ia
    pro log). Agora só quem está logado e configura a escola baixa, e nada mais é público."""
    from flask import g

    from ..auth.rbac import tem_permissao, unidade_atual_ou_403

    if current_app.config.get("MODO") != "nuvem":
        abort(404)
    un = unidade_atual_ou_403()
    if un["id"] != unidade_id or not tem_permissao(g.usuario, "gerir_dispositivos"):
        abort(404)
    if not (un.get("config") or {}).get("espelho_token"):
        abort(409)
    ps1 = instalador_ps1(un, current_app.config["BASE_URL"])
    if _MARCA_PS1 in ps1:
        abort(500)
    nome = "".join(c for c in un["nome"] if c.isascii() and (c.isalnum() or c in "-_")) or "unidade"
    extrair = (
        f"$m='{_MARCA_PS1}'; $t=[IO.File]::ReadAllText('%~f0'); $i=$t.LastIndexOf($m); "
        "$d=Join-Path $env:ProgramData 'nexosix-inst'; New-Item -ItemType Directory -Force $d | Out-Null; "
        "icacls $d /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null; "
        "$p=Join-Path $d ('inst-'+[guid]::NewGuid()+'.ps1'); [IO.File]::WriteAllText($p, $t.Substring($i+$m.Length), [Text.Encoding]::UTF8); "
        "try { & $p } finally { Remove-Item $p -Force -ErrorAction SilentlyContinue }"
    )
    auditar("baixar_instalador", "unidade", un["id"])
    corpo = (
        "@echo off\r\n"
        f"title Instalador do agente Nexosix - {nome}\r\n"
        "net session >nul 2>&1 || (echo Pedindo permissao de administrador... & powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\" & exit /b)\r\n"
        f"powershell -NoProfile -ExecutionPolicy Bypass -Command \"{extrair}\"\r\n"
        "echo.\r\npause\r\nexit /b\r\n"
        f"{_MARCA_PS1}\r\n{ps1}"
    )
    return Response(corpo, content_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename=instalar-agente-{nome}.cmd", "Cache-Control": "no-store"})


# O que vai pro PC da escola (sem testes, documentos, .git)
_PACOTE = ("app", "worker", "integrations", "migrations", "deploy", "serve.py", "wsgi.py", "requirements.txt")


def pacote_zip():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for item in _PACOTE:
            caminho = os.path.join(_RAIZ, item)
            if os.path.isfile(caminho):
                z.write(caminho, f"nexosix/{item}")
                continue
            for base, pastas, arquivos in os.walk(caminho):
                pastas[:] = [p for p in pastas if p != "__pycache__"]
                for a in arquivos:
                    if a.endswith((".pyc", ".env")) or a == ".env":
                        continue
                    completo = os.path.join(base, a)
                    z.write(completo, "nexosix/" + os.path.relpath(completo, _RAIZ).replace(os.sep, "/"))
    return buf.getvalue()


@bp.get("/api/espelho/pacote.zip")
def pacote():
    """Código do agente, servido pela nuvem (com o token no cabeçalho). Antes o instalador
    baixava do GitHub, o que obrigava o repositório a ser público."""
    _unidade_do_agente()
    return Response(pacote_zip(), content_type="application/zip", headers={"Content-Disposition": "attachment; filename=nexosix-agente.zip"})


# ---------------------------------------------------------------- agente (MODO=local)

@bp.get("/agente")
def agente_status():
    if current_app.config.get("MODO") != "local":
        abort(404)
    est = service.estado_local()
    pend = service.pendentes()
    disps = db.query("SELECT nome, status, ultimo_contato, modo FROM dispositivos WHERE ativo ORDER BY nome")
    un = db.query("SELECT nome FROM unidades WHERE id = %s", (current_app.config.get("UNIDADE_ID"),), one=True)
    fila = db.query("SELECT status, count(*) AS n FROM fila_sync GROUP BY status")
    quarentena = db.query("SELECT count(*) AS n FROM espelho_quarentena", one=True)["n"]
    return render_template("agente.html", estado=est, pendentes=pend, dispositivos=disps, unidade=un, fila=fila, quarentena=quarentena,
                           unidade_id=current_app.config.get("UNIDADE_ID"), nuvem=current_app.config.get("NUVEM_URL"))


def instalar_agente_local(app):
    """O agente não tem painel: tudo que não é API da catraca vai pra nuvem."""
    livres = ("/api/", "/static/", "/health", "/agente")

    @app.before_request
    def _so_catraca():
        if request.path == "/" or any(request.path.startswith(p) for p in livres):
            return None
        nuvem = app.config.get("NUVEM_URL")
        return redirect(nuvem + request.path) if nuvem else abort(404)
