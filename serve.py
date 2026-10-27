"""Servidor web do Modelo 3 no Windows (gunicorn não roda no Windows; waitress sim).

    python serve.py            # escuta em 0.0.0.0:8000
    PORT=8080 python serve.py
"""
import os

from waitress import serve

from app import create_app

if __name__ == "__main__":
    app = create_app()
    porta = int(os.environ.get("PORT", "80" if app.config.get("MODO") == "local" else "8000"))
    print(f"Nexosix Acesso ({app.config.get('MODO')}) em http://0.0.0.0:{porta}", flush=True)
    serve(app, host="0.0.0.0", port=porta, threads=8)
