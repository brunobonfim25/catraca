"""Login/logout, troca de unidade, gestão de usuários do painel."""
from urllib.parse import urlparse

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import db
from ..auditoria import auditar
from ..util import caminho_interno
from .rbac import PERFIS, ROTULO_PERFIL, carregar_usuario, exige, home_de, login_obrigatorio, unidades_visiveis, pode_ver_unidade

bp = Blueprint("auth", __name__)


@bp.before_app_request
def _carregar():
    carregar_usuario()


@bp.app_context_processor
def _injetar():
    from .rbac import tem_permissao

    u = getattr(g, "usuario", None)
    return {
        "usuario": u,
        "unidade": getattr(g, "unidade", None),
        "tem_permissao": lambda p: tem_permissao(u, p),
        "rotulo_perfil": ROTULO_PERFIL.get(u["perfil"], u["perfil"]) if u else "",
        "escolas_visiveis": unidades_visiveis(u) if u and not u["unidade_id"] else [],
    }


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        u = db.query("SELECT * FROM usuarios_admin WHERE lower(email) = %s AND ativo", (email,), one=True)
        if u and check_password_hash(u["senha_hash"], senha):
            session.clear()
            session.permanent = True
            session["usuario_id"] = u["id"]
            db.execute("UPDATE usuarios_admin SET ultimo_login = now() WHERE id = %s", (u["id"],))
            # Usuário de uma unidade só já entra nela; os outros escolhem
            if u["unidade_id"]:
                session["unidade_id"] = u["unidade_id"]
            else:
                visiveis = unidades_visiveis(u)
                if len(visiveis) == 1:
                    session["unidade_id"] = visiveis[0]["id"]
            return redirect(caminho_interno(request.args.get("proximo"), home_de(u)))
        flash("E-mail ou senha inválidos", "danger")
    return render_template("auth/login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/unidade/<int:unidade_id>")
@login_obrigatorio
def trocar_unidade(unidade_id):
    un = db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)
    if un and pode_ver_unidade(g.usuario, un):
        session["unidade_id"] = unidade_id
    volta = urlparse(request.referrer or "")
    destino = volta.path + (f"?{volta.query}" if volta.query else "") if volta.netloc == request.host else ""
    return redirect(caminho_interno(destino, url_for("painel.dashboard")))


@bp.route("/usuarios")
@exige("gerir_usuarios")
def usuarios():
    if g.usuario["perfil"] == "superadmin":
        rows = db.query("SELECT u.*, un.nome AS unidade_nome FROM usuarios_admin u LEFT JOIN unidades un ON un.id = u.unidade_id ORDER BY u.nome")
    elif g.usuario["unidade_id"]:  # direção de uma escola vê só a própria escola
        rows = db.query(
            "SELECT u.*, un.nome AS unidade_nome FROM usuarios_admin u LEFT JOIN unidades un ON un.id = u.unidade_id "
            "WHERE u.unidade_id = %s AND u.perfil <> 'superadmin' ORDER BY u.nome", (g.usuario["unidade_id"],),
        )
    else:
        rows = db.query(
            "SELECT u.*, un.nome AS unidade_nome FROM usuarios_admin u LEFT JOIN unidades un ON un.id = u.unidade_id "
            "WHERE u.empresa_id = %s AND u.perfil <> 'superadmin' ORDER BY u.nome", (g.usuario["empresa_id"],),
        )
    return render_template("auth/usuarios.html", usuarios=rows, perfis=PERFIS, rotulos=ROTULO_PERFIL, unidades=unidades_visiveis(g.usuario))


@bp.route("/usuarios/novo", methods=["POST"])
@exige("gerir_usuarios")
def usuario_novo():
    f = request.form
    perfil = f.get("perfil")
    if perfil not in PERFIS or (perfil == "superadmin" and g.usuario["perfil"] != "superadmin"):
        flash("Perfil inválido", "danger")
        return redirect(url_for("auth.usuarios"))
    if len(f.get("senha") or "") < 8:
        flash("A senha inicial precisa de 8 caracteres ou mais", "danger")
        return redirect(url_for("auth.usuarios"))
    unidade_id = int(f["unidade_id"]) if f.get("unidade_id") else None
    empresa_id = g.usuario["empresa_id"]
    # Quem é de uma escola só cria usuário da própria escola. Antes, deixar o campo vazio
    # criava um usuário da rede inteira, que entrava nas outras escolas.
    if g.usuario["unidade_id"]:
        unidade_id = g.usuario["unidade_id"]
    if unidade_id:
        un = db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)
        if not un or not pode_ver_unidade(g.usuario, un):
            flash("Unidade inválida", "danger")
            return redirect(url_for("auth.usuarios"))
        empresa_id = un["empresa_id"]
    row = db.execute(
        "INSERT INTO usuarios_admin (empresa_id, unidade_id, nome, email, senha_hash, perfil) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (empresa_id, unidade_id, f["nome"].strip(), f["email"].strip().lower(), generate_password_hash(f["senha"]), perfil),
        returning=True,
    )
    auditar("criar", "usuarios_admin", row["id"], depois={"nome": f["nome"], "email": f["email"], "perfil": perfil})
    flash("Usuário criado", "success")
    return redirect(url_for("auth.usuarios"))


@bp.route("/usuarios/<int:uid>/ativo", methods=["POST"])
@exige("gerir_usuarios")
def usuario_ativo(uid):
    u = db.query("SELECT * FROM usuarios_admin WHERE id = %s", (uid,), one=True)
    if not u or uid == g.usuario["id"]:
        return redirect(url_for("auth.usuarios"))
    if g.usuario["perfil"] != "superadmin":
        # dentro da rede: diretor de escola só mexe em usuário da própria escola, nunca na Nexosix
        if u["empresa_id"] != g.usuario["empresa_id"] or u["perfil"] == "superadmin":
            return redirect(url_for("auth.usuarios"))
        if g.usuario["unidade_id"] and u["unidade_id"] != g.usuario["unidade_id"]:
            return redirect(url_for("auth.usuarios"))
    db.execute("UPDATE usuarios_admin SET ativo = NOT ativo WHERE id = %s", (uid,))
    auditar("ativar_desativar", "usuarios_admin", uid, antes={"ativo": u["ativo"]}, depois={"ativo": not u["ativo"]})
    return redirect(url_for("auth.usuarios"))


@bp.route("/senha", methods=["GET", "POST"])
@login_obrigatorio
def trocar_senha():
    if request.method == "POST":
        atual = request.form.get("atual") or ""
        nova = request.form.get("nova") or ""
        if not check_password_hash(g.usuario["senha_hash"], atual):
            flash("Senha atual incorreta", "danger")
        elif len(nova) < 8:
            flash("A nova senha precisa de 8 caracteres ou mais", "danger")
        else:
            db.execute("UPDATE usuarios_admin SET senha_hash = %s WHERE id = %s", (generate_password_hash(nova), g.usuario["id"]))
            auditar("trocar_senha", "usuarios_admin", g.usuario["id"])
            flash("Senha alterada", "success")
            return redirect(url_for("painel.dashboard"))
    return render_template("auth/senha.html")
