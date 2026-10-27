"""Perfis e decorators de permissão.

Perfis:
  superadmin     — Nexosix; vê todas as empresas/escolas
  admin_unidade  — direção/TI da escola; cadastra tudo, muda regras e catracas
  secretaria     — cadastra alunos, responsáveis, turmas, fotos, Telegram; vê presença
  recepcao       — opera o dia a dia: espera de responsáveis, liberações manuais, visitantes
  coordenacao    — vê presença, atrasos, relatórios; não libera catraca
  auditor        — só leitura (auditoria, relatórios)
"""
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

from .. import db

PERFIS = ("superadmin", "admin_unidade", "secretaria", "recepcao", "coordenacao", "auditor")
ROTULO_PERFIL = {"superadmin": "Nexosix", "admin_unidade": "Direção", "secretaria": "Secretaria", "recepcao": "Recepção", "coordenacao": "Coordenação", "auditor": "Auditor"}

# Permissões nomeadas → perfis que as têm. Centralizado pra regra não se espalhar nas rotas.
PERMISSOES = {
    "cadastrar_pessoas": {"superadmin", "admin_unidade", "secretaria", "recepcao"},
    "gerir_turmas": {"superadmin", "admin_unidade", "secretaria"},
    "gerir_dispositivos": {"superadmin", "admin_unidade"},
    "gerir_usuarios": {"superadmin", "admin_unidade"},
    "configurar_escola": {"superadmin", "admin_unidade"},
    "liberar_manual": {"superadmin", "admin_unidade", "recepcao"},
    "ver_presenca": {"superadmin", "admin_unidade", "secretaria", "recepcao", "coordenacao", "auditor"},
    "ver_relatorios": {"superadmin", "admin_unidade", "coordenacao", "auditor"},
    "ver_auditoria": {"superadmin", "admin_unidade", "auditor"},
    "excluir_lgpd": {"superadmin", "admin_unidade"},
}

# Tela inicial de cada papel (nome do endpoint Flask)
# Tela inicial por papel: cada um cai onde trabalha (secretaria em Alunos, recepção na Recepção).
HOME_POR_PERFIL = {
    "superadmin": ("painel.rede", {}), "admin_unidade": ("painel.dashboard", {}), "secretaria": ("pessoas.lista", {"tipo": "aluno"}),
    "recepcao": ("painel.recepcao", {}), "coordenacao": ("painel.dashboard", {}), "auditor": ("painel.dashboard", {}),
}


def home_de(usuario):
    """URL da tela inicial do usuário."""
    from flask import url_for

    endpoint, args = HOME_POR_PERFIL.get(usuario["perfil"], ("painel.dashboard", {}))
    return url_for(endpoint, **args)


def versao_senha(u):
    """Pedaço do hash da senha guardado na sessão: mudou a senha, as sessões antigas caem."""
    import hashlib

    return hashlib.sha256((u.get("senha_hash") or "").encode()).hexdigest()[:16]


def carregar_usuario():
    """Preenche g.usuario e g.unidade a partir da sessão. Retorna None se deslogado."""
    uid = session.get("usuario_id")
    # Cache por requisição, mas só se ainda é o mesmo login: com app_context compartilhado
    # (testes, CLI) o g sobrevive entre requisições e deixaria um logout "logado".
    cache = getattr(g, "usuario", None)
    if (cache is not None and cache["id"] == uid and session.get("unidade_id") == getattr(g, "unidade_id_cache", None)
            and session.get("senha_v") == getattr(g, "senha_v_cache", None)):
        return cache
    g.unidade_id_cache = session.get("unidade_id")
    g.senha_v_cache = session.get("senha_v")
    if not uid:
        g.usuario = None
        g.unidade = None
        return None
    u = db.query("SELECT * FROM usuarios_admin WHERE id = %s AND ativo", (uid,), one=True)
    if u and session.get("senha_v") != versao_senha(u):
        u = None  # senha trocada (ou sessão de antes desta regra): entrar de novo
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
