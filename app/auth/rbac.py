"""Perfis e decorators de permissão.

Perfis:
  superadmin     — Nexosix; vê todas as empresas/unidades
  admin_unidade  — direção/TI da escola; cadastra tudo, muda regras e dispositivos
  recepcao       — opera o dia a dia: espera de responsáveis, liberações manuais, visitantes
  coordenacao    — vê presença, atrasos, relatórios; não libera catraca
  auditor        — só leitura (auditoria, relatórios)
"""
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

from .. import db

PERFIS = ("superadmin", "admin_unidade", "recepcao", "coordenacao", "auditor")

# Permissões nomeadas → perfis que as têm. Centralizado pra regra não se espalhar nas rotas.
PERMISSOES = {
    "cadastrar_pessoas": {"superadmin", "admin_unidade", "recepcao"},
    "gerir_turmas": {"superadmin", "admin_unidade"},
    "gerir_dispositivos": {"superadmin", "admin_unidade"},
    "gerir_usuarios": {"superadmin", "admin_unidade"},
    "liberar_manual": {"superadmin", "admin_unidade", "recepcao"},
    "ver_presenca": {"superadmin", "admin_unidade", "recepcao", "coordenacao", "auditor"},
    "ver_relatorios": {"superadmin", "admin_unidade", "coordenacao", "auditor"},
    "ver_auditoria": {"superadmin", "admin_unidade", "auditor"},
    "excluir_lgpd": {"superadmin", "admin_unidade"},
}


def carregar_usuario():
    """Preenche g.usuario e g.unidade a partir da sessão. Retorna None se deslogado."""
    uid = session.get("usuario_id")
    # Cache por requisição, mas só se ainda é o mesmo login: com app_context compartilhado
    # (testes, CLI) o g sobrevive entre requisições e deixaria um logout "logado".
    cache = getattr(g, "usuario", None)
    if cache is not None and cache["id"] == uid and session.get("unidade_id") == getattr(g, "unidade_id_cache", None):
        return cache
    g.unidade_id_cache = session.get("unidade_id")
    if not uid:
        g.usuario = None
        g.unidade = None
        return None
    u = db.query("SELECT * FROM usuarios_admin WHERE id = %s AND ativo", (uid,), one=True)
    if not u:
        session.clear()
        g.usuario = None
        g.unidade = None
        return None
    g.usuario = u
    unidade_id = session.get("unidade_id") or u["unidade_id"]
    g.unidade = None
    if unidade_id:
        un = db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)
        if un and pode_ver_unidade(u, un):
            g.unidade = un
    return u


def pode_ver_unidade(usuario, unidade):
    if usuario["perfil"] == "superadmin":
        return True
    if usuario["unidade_id"]:
        return usuario["unidade_id"] == unidade["id"]
    return usuario["empresa_id"] == unidade["empresa_id"]


def unidades_visiveis(usuario):
    if usuario["perfil"] == "superadmin":
        return db.query("SELECT u.*, e.nome AS empresa FROM unidades u JOIN empresas e ON e.id = u.empresa_id WHERE u.ativo ORDER BY e.nome, u.nome")
    if usuario["unidade_id"]:
        return db.query("SELECT u.*, e.nome AS empresa FROM unidades u JOIN empresas e ON e.id = u.empresa_id WHERE u.id = %s", (usuario["unidade_id"],))
    return db.query(
        "SELECT u.*, e.nome AS empresa FROM unidades u JOIN empresas e ON e.id = u.empresa_id WHERE u.empresa_id = %s AND u.ativo ORDER BY u.nome",
        (usuario["empresa_id"],),
    )


def tem_permissao(usuario, permissao):
    return usuario is not None and usuario["perfil"] in PERMISSOES.get(permissao, set())


def login_obrigatorio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if carregar_usuario() is None:
            return redirect(url_for("auth.login", proximo=request.path))
        return f(*args, **kwargs)

    return wrapper


def exige(permissao):
    """Exige login + permissão nomeada. Fora da permissão responde 403."""

    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = carregar_usuario()
            if u is None:
                return redirect(url_for("auth.login", proximo=request.path))
            if not tem_permissao(u, permissao):
                abort(403)
            return f(*args, **kwargs)

        return wrapper

    return deco


def unidade_atual_ou_403():
    """A maioria das telas opera numa unidade. Sem unidade selecionada → escolher."""
    carregar_usuario()
    if g.unidade is None:
        abort(403, "Selecione uma unidade")
    return g.unidade
