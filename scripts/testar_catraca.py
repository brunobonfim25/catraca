"""Primeiro contato com a catraca real: só leitura, mais acerto de hora (opcional).

    python -m scripts.testar_catraca --ip 192.168.0.50 --senha SENHA [--acertar-hora]

Imprime o que a doc promete e marca o que divergir. Anote o resultado em
integrations/controlid/README.md (coluna Hardware).
"""
import argparse
import json
from datetime import datetime, timezone

from integrations.base_adapter import DispositivoErro
from integrations.controlid.adapter import ControlIdAdapter


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ip", required=True)
    p.add_argument("--porta", type=int, default=80)
    p.add_argument("--usuario", default="admin")
    p.add_argument("--senha", required=True)
    p.add_argument("--acertar-hora", action="store_true")
    a = p.parse_args()

    ad = ControlIdAdapter(a.ip, a.porta, a.usuario, a.senha, timeout=10)
    passos = [
        ("login.fcgi", lambda: ad.connect()[:6] + "…"),
        ("session_is_valid.fcgi", ad.health),
        ("system_information.fcgi", lambda: json.dumps(ad.identify_device(), ensure_ascii=False)),
        ("load_objects portals", lambda: ad.load_objects("portals")),
        ("load_objects users (3 primeiros)", lambda: ad.load_objects("users", fields=["id", "name", "registration"], limit=3)),
        ("load_objects access_rules", lambda: ad.load_objects("access_rules", fields=["id", "name", "type"])),
        ("load_objects access_logs (últimos 3)", lambda: ad.load_objects("access_logs", order=["id", "descending"], limit=3)),
        ("user_list_images", lambda: f"{len(ad.list_user_images())} fotos"),
    ]
    if a.acertar_hora:
        passos.append(("set_system_time.fcgi", lambda: ad.set_clock(datetime.now(timezone.utc)) or "ok"))
    for nome, fn in passos:
        try:
            print(f"✔ {nome}: {fn()}")
        except DispositivoErro as e:
            print(f"✘ {nome}: {e}")
        except Exception as e:  # noqa: BLE001 — é um roteiro de diagnóstico
            print(f"✘ {nome}: erro inesperado {type(e).__name__}: {e}")
    print("\nPróximo: cadastrar a catraca no painel com este IP, 'Testar conexão', depois 'Configurar Monitor'.")


if __name__ == "__main__":
    main()
