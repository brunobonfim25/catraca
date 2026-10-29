"""Nuvem de teste para o teste ponta a ponta do instalador (testar.ps1, só no GitHub Actions).

    python ci_nuvem.py semear      → cria rede, escola (com acento no nome) e token da catraca
    python ci_nuvem.py codigo      → imprime um código de instalação novo
    python ci_nuvem.py visto       → imprime agente_visto_em da escola (vazio = ainda não)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from app import create_app, db  # noqa: E402

ESCOLA = "Escola Teste Itaguaí"

app = create_app()
with app.app_context():
    acao = sys.argv[1]
    if acao == "semear":
        db.migrar(log=print)
        emp = db.execute("INSERT INTO empresas (nome) VALUES ('Rede CI') RETURNING id", returning=True)["id"]
        db.execute("INSERT INTO unidades (empresa_id, nome, config) VALUES (%s, %s, '{\"monitor_token\": \"tok-ci\"}')", (emp, ESCOLA))
        print("ok")
    elif acao == "codigo":
        from app.espelho import instalacao

        uid = db.query("SELECT id FROM unidades WHERE nome = %s", (ESCOLA,), one=True)["id"]
        print(instalacao.gerar(uid)[0])
    elif acao == "visto":
        r = db.query("SELECT config->>'agente_visto_em' AS v FROM unidades WHERE nome = %s", (ESCOLA,), one=True)
        print(r["v"] or "")
