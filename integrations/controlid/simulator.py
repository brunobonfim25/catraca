"""Simulador HTTP de uma catraca Control iD (idBlock Next com iDFace).

Reproduz SÓ o comportamento documentado em https://www.controlid.com.br/docs/access-api-pt/
(login, objetos, fotos, ações, Monitor, modo online). Onde a doc é omissa, o comportamento
escolhido está marcado [SUP] e precisa ser confirmado no equipamento — ver README.md.

Uso em dev (uma catraca fake na porta 8085, avisando o servidor local):
    python -m integrations.controlid.simulator --porta 8085 --device-id 935107

Endpoints extras (não existem na catraca real, só pra provocar eventos):
    POST /sim/identificar   {"user_id": 5, "component_id": "0MS1", "girar": true}
    POST /sim/botoeira
    GET  /sim/estado
"""
import argparse
import base64
import secrets
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, request

OBJETOS = (
    "users", "user_roles", "groups", "user_groups", "access_rules", "group_access_rules",
    "user_access_rules", "portal_access_rules", "time_zones", "time_spans", "access_rule_time_zones",
    "portals", "devices", "access_logs", "custom_thresholds",
)

# [DOC] portais ligam áreas; num idBlock o padrão de fábrica tem entrada e saída.
# [SUP] ids 1 e 2 — confirmar com load_objects portals no equipamento real.
PORTAIS_PADRAO = [
    {"id": 1, "name": "Entrada", "area_from_id": 1, "area_to_id": 2},
    {"id": 2, "name": "Saída", "area_from_id": 2, "area_to_id": 1},
]


class EstadoCatraca:
    def __init__(self, device_id=935107, serial="SIM-0001", login="admin", senha="admin"):
        self.device_id = device_id
        self.serial = serial
        self.login = login
        self.senha = senha
        self.sessoes = set()
        self.tabelas = {o: [] for o in OBJETOS}
        self.tabelas["portals"] = [dict(p) for p in PORTAIS_PADRAO]
        self.proximo_id = {o: 1 for o in OBJETOS}
        self.proximo_id["portals"] = 3
        self.imagens = {}  # user_id → bytes
        self.config = {
            "general": {"online": "0", "local_identification": "1", "ihm_enterprise_mode": "0"},
            "online_client": {"server_id": "", "extract_template": "0", "max_request_attempts": "3"},
            "monitor": {},
            "sec_box": {"catra_role": "1", "catra_side_to_enter": "0", "catra_timeout": "1000"},
        }
        self.relogio_offset = 0
        self.liberacoes = []  # histórico de execute_actions
        self.modo_contingencia = False
        self.lock = threading.RLock()
        self.monitor_falhas = []  # erros ao avisar o servidor (pra depuração)

    def reset(self):
        """Volta ao estado de fábrica (testes). Mantém device_id/serial/login."""
        with self.lock:
            self.sessoes.clear()
            for o in OBJETOS:
                self.tabelas[o] = []
                self.proximo_id[o] = 1
            self.tabelas["portals"] = [dict(p) for p in PORTAIS_PADRAO]
            self.proximo_id["portals"] = 3
            self.imagens.clear()
            self.config = {
                "general": {"online": "0", "local_identification": "1", "ihm_enterprise_mode": "0"},
                "online_client": {"server_id": "", "extract_template": "0", "max_request_attempts": "3"},
                "monitor": {},
                "sec_box": {"catra_role": "1", "catra_side_to_enter": "0", "catra_timeout": "1000"},
            }
            self.relogio_offset = 0
            self.liberacoes.clear()
            self.modo_contingencia = False
            self.monitor_falhas.clear()

    # ---- helpers
    def agora(self):
        return int(time.time()) + self.relogio_offset

    def novo_id(self, objeto):
        i = self.proximo_id[objeto]
        self.proximo_id[objeto] = i + 1
        return i

    def monitor_url(self, endpoint):
        m = self.config["monitor"]
        if not m.get("hostname"):
            return None
        porta = m.get("port") or "80"
        path = (m.get("path") or "api/notifications").strip("/")
        return f"http://{m['hostname']}:{porta}/{path}/{endpoint}"

    def servidor_online_url(self):
        sid = self.config["online_client"].get("server_id")
        if self.config["general"].get("online") != "1" or not sid:
            return None
        for d in self.tabelas["devices"]:
            if str(d["id"]) == str(sid):
                return d["ip"].rstrip("/")
        return None


def _filtra(linhas, where, objeto):
    """Implementa o subconjunto do `where` que usamos: {objeto: {campo: valor | {op: valor}}}."""
    if not where:
        return list(linhas)
    cond = where.get(objeto, {}) if isinstance(where, dict) else {}
    out = []
    for l in linhas:
        ok = True
        for campo, esperado in cond.items():
            v = l.get(campo)
            if isinstance(esperado, dict):
                for op, alvo in esperado.items():
                    if op in ("=", "==", "LIKE") and not v == alvo:
                        ok = False
                    elif op in ("!=", "<>", "NOT LIKE") and v == alvo:
                        ok = False
                    elif op == ">" and not (v is not None and v > alvo):
                        ok = False
                    elif op == "<" and not (v is not None and v < alvo):
                        ok = False
                    elif op == ">=" and not (v is not None and v >= alvo):
                        ok = False
                    elif op == "<=" and not (v is not None and v <= alvo):
                        ok = False
                    elif op == "IN" and v not in alvo:
                        ok = False
                    elif op == "NOT IN" and v in alvo:
                        ok = False
            elif v != esperado:
                ok = False
        if ok:
            out.append(l)
    return out


def criar_simulador(estado=None):
    estado = estado or EstadoCatraca()
    app = Flask("simulador_controlid")
    app.estado = estado

    def exige_sessao():
        s = request.args.get("session")
        if not s or s not in estado.sessoes:
            # [SUP] a doc não diz o código de erro de sessão inválida; usamos 401
            return jsonify({"error": "invalid session"}), 401
        return None

    # ---------------------------------------------------------------- sessão
    @app.post("/login.fcgi")
    def login():
        corpo = request.get_json(silent=True) or {}
        if corpo.get("login", "admin") != estado.login or corpo.get("password", "admin") != estado.senha:
            return jsonify({"error": "invalid credentials"}), 401
        s = secrets.token_urlsafe(18)
        estado.sessoes.add(s)
        return jsonify({"session": s})

    @app.post("/session_is_valid.fcgi")
    def session_is_valid():
        return jsonify({"session_is_valid": request.args.get("session") in estado.sessoes})

    @app.post("/logout.fcgi")
    def logout():
        estado.sessoes.discard(request.args.get("session"))
        return ""

    # ---------------------------------------------------------------- objetos
    @app.post("/create_objects.fcgi")
    def create_objects():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        objeto = corpo["object"]
        if objeto not in OBJETOS:
            return jsonify({"error": f"unknown object {objeto}"}), 400
        ids = []
        with estado.lock:
            for valores in corpo.get("values", []):
                linha = dict(valores)
                if objeto == "user_access_rules":
                    # [DOC] par (user_id, access_rule_id) é único
                    if any(l["user_id"] == linha["user_id"] and l["access_rule_id"] == linha["access_rule_id"] for l in estado.tabelas[objeto]):
                        return jsonify({"error": "duplicate user_access_rules"}), 400
                if "id" not in linha and objeto not in ("user_roles", "user_groups", "group_access_rules", "user_access_rules", "portal_access_rules", "access_rule_time_zones"):
                    linha["id"] = estado.novo_id(objeto)
                if objeto == "users":
                    linha.setdefault("begin_time", 0)
                    linha.setdefault("end_time", 0)
                    linha.setdefault("image_timestamp", 0)
                    linha.setdefault("last_access", 0)
                    linha.setdefault("user_type_id", None)
                estado.tabelas[objeto].append(linha)
                ids.append(linha.get("id"))
        return jsonify({"ids": ids})

    @app.post("/load_objects.fcgi")
    def load_objects():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        objeto = corpo["object"]
        if objeto not in OBJETOS:
            return jsonify({"error": f"unknown object {objeto}"}), 400
        with estado.lock:
            linhas = _filtra(estado.tabelas[objeto], corpo.get("where"), objeto)
        ordem = corpo.get("order") or []
        campos_ordem = [c for c in ordem if c not in ("ascending", "descending")]
        if campos_ordem:
            linhas.sort(key=lambda l: tuple(l.get(c) or 0 for c in campos_ordem), reverse=("descending" in ordem))
        if corpo.get("limit") is not None:
            ini = corpo.get("offset") or 0
            linhas = linhas[ini: ini + int(corpo["limit"])]
        if corpo.get("fields"):
            linhas = [{k: l.get(k) for k in corpo["fields"]} for l in linhas]
        return jsonify({objeto: linhas})

    @app.post("/modify_objects.fcgi")
    def modify_objects():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        objeto = corpo["object"]
        n = 0
        with estado.lock:
            for l in _filtra(estado.tabelas[objeto], corpo.get("where"), objeto):
                l.update(corpo.get("values", {}))
                n += 1
        return jsonify({"changes": n})

    @app.post("/destroy_objects.fcgi")
    def destroy_objects():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        objeto = corpo["object"]
        with estado.lock:
            alvo = _filtra(estado.tabelas[objeto], corpo.get("where"), objeto)
            estado.tabelas[objeto] = [l for l in estado.tabelas[objeto] if l not in alvo]
            if objeto == "users":
                for l in alvo:
                    estado.imagens.pop(l.get("id"), None)
        return jsonify({"changes": len(alvo)})

    # ---------------------------------------------------------------- fotos
    def _valida_imagem(dados):
        """[DOC] critérios: JPG/PNG, rosto detectado, centrado, nitidez… O simulador só
        checa formato e tamanho; um marcador 'SEMROSTO' no conteúdo força o erro 2 (testes)."""
        if len(dados) >= 2 * 1024 * 1024:
            return [{"code": 1, "message": "Image too large"}]
        if not (dados[:2] == b"\xff\xd8" or dados[:8] == b"\x89PNG\r\n\x1a\n"):
            return [{"code": 1, "message": "Image file not recognized. Image should be either JPG or PNG."}]
        if b"SEMROSTO" in dados:
            return [{"code": 2, "message": "Face not detected"}]
        return []

    scores_ok = {"bounds_width": 300, "horizontal_center_offset": 10, "vertical_center_offset": 12, "center_pose_quality": 100, "sharpness_quality": 900}

    @app.post("/user_test_image.fcgi")
    def user_test_image():
        if (e := exige_sessao()):
            return e
        erros = _valida_imagem(request.get_data())
        return jsonify({"success": not erros, "errors": erros, "scores": scores_ok})

    @app.post("/user_set_image.fcgi")
    def user_set_image():
        if (e := exige_sessao()):
            return e
        user_id = int(request.args.get("user_id", 0))
        dados = request.get_data()
        with estado.lock:
            usuario = next((u for u in estado.tabelas["users"] if u["id"] == user_id), None)
            if not usuario:
                return jsonify({"user_id": user_id, "success": False, "errors": [{"code": 1, "message": "User does not exist"}], "scores": {}})
            erros = _valida_imagem(dados)
            if not erros and request.args.get("match") == "1":
                # [DOC] match=1: recusa se o rosto já pertence a outro usuário (code 3)
                for outro, img in estado.imagens.items():
                    if outro != user_id and img == dados:
                        return jsonify({"user_id": user_id, "success": False, "errors": [{"code": 3, "message": "Face exists"}], "info": {"match_user_id": outro}, "scores": scores_ok})
            if erros:
                return jsonify({"user_id": user_id, "success": False, "errors": erros, "scores": {}})
            estado.imagens[user_id] = dados
            usuario["image_timestamp"] = int(request.args.get("timestamp") or estado.agora())
        return jsonify({"user_id": user_id, "success": True, "errors": [], "scores": scores_ok})

    @app.get("/user_get_image.fcgi")
    def user_get_image():
        if (e := exige_sessao()):
            return e
        img = estado.imagens.get(int(request.args.get("user_id", 0)))
        if img is None:
            return Response(status=404)
        return Response(img, content_type="image/jpeg")

    @app.route("/user_list_images.fcgi", methods=["GET", "POST"])
    def user_list_images():
        if (e := exige_sessao()):
            return e
        return jsonify({"user_ids": sorted(estado.imagens)})

    @app.post("/user_destroy_image.fcgi")
    def user_destroy_image():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True) or {}
        with estado.lock:
            if corpo.get("all"):
                estado.imagens.clear()
            for uid in [corpo.get("user_id")] + list(corpo.get("user_ids") or []):
                if uid is not None:
                    estado.imagens.pop(int(uid), None)
        return ""

    @app.post("/user_set_image_list.fcgi")
    def user_set_image_list():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        resultados = []
        for item in corpo.get("user_images", []):
            dados = base64.b64decode(item["image"])
            uid = int(item["user_id"])
            erros = _valida_imagem(dados)
            if not erros:
                estado.imagens[uid] = dados
            resultados.append({"user_id": uid, "success": not erros, "errors": erros, "scores": scores_ok})
        return jsonify({"results": resultados})

    # ---------------------------------------------------------------- sistema
    @app.post("/system_information.fcgi")
    def system_information():
        if (e := exige_sessao()):
            return e
        return jsonify({
            "uptime": {"days": 1, "hours": 2, "minutes": 3, "seconds": 4},
            "time": estado.agora(),
            "memory": {"disk": {"free": 1000000, "total": 2000000}, "ram": {"free": 500000, "total": 1000000}},
            "license": {"users": 3000, "device": 1, "type": 1},
            "biometrics": {"max_num_records": 3000},
            "network": {"mac": "00:11:22:33:44:55", "ip": "127.0.0.1", "netmask": "255.255.255.0", "gateway": "127.0.0.1", "web_server_port": 80, "ssl_enabled": False, "dhcp_enabled": False, "ten_mbps": False, "dns_primary": "", "dns_secondary": ""},
            "serial": estado.serial,
            "version": "6.50.0-sim",
            "device_id": str(estado.device_id),
            "secbox_version": "1.0",
            "iDCloud_code": "",
            "online": estado.config["general"].get("online") == "1",
            "online_available": True,
        })

    @app.post("/set_system_time.fcgi")
    def set_system_time():
        if (e := exige_sessao()):
            return e
        c = request.get_json(force=True)
        alvo = datetime(c["year"], c["month"], c["day"], c["hour"], c["minute"], c["second"], tzinfo=timezone.utc)
        estado.relogio_offset = int(alvo.timestamp()) - int(time.time())
        return ""

    @app.post("/set_configuration.fcgi")
    def set_configuration():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        with estado.lock:
            for modulo, chaves in corpo.items():
                estado.config.setdefault(modulo, {}).update({k: str(v) for k, v in chaves.items()})
        return ""

    @app.post("/get_configuration.fcgi")
    def get_configuration():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True) or {}
        return jsonify({m: {k: estado.config.get(m, {}).get(k) for k in chaves} for m, chaves in corpo.items()})

    # ---------------------------------------------------------------- ações
    @app.post("/execute_actions.fcgi")
    def execute_actions():
        if (e := exige_sessao()):
            return e
        corpo = request.get_json(force=True)
        for acao in corpo.get("actions", []):
            estado.liberacoes.append({"action": acao.get("action"), "parameters": acao.get("parameters"), "time": estado.agora()})
            if acao.get("action") == "catra":
                # [DOC] liberação pela API gera evento 10 (acesso não identificado)
                _gravar_log(event=10, user_id=0, portal_id=_portal_por_allow(acao.get("parameters", "")))
        return ""

    # ---------------------------------------------------------------- motor interno
    def _portal_por_allow(parametros):
        if "anticlockwise" in parametros:
            return 2
        if "clockwise" in parametros:
            return 1
        return 1

    def _portal_por_component(component_id):
        # [SUP] 0MS1 (primário) = leitor de fora = entrada (portal 1); 0MS2 = dentro = saída (portal 2)
        return 2 if component_id == "0MS2" else 1

    def _gravar_log(event, user_id, portal_id, extra=None):
        with estado.lock:
            log = {
                "id": estado.novo_id("access_logs"),
                "time": estado.agora(),
                "event": event,
                "device_id": estado.device_id,
                "identifier_id": 1,
                "user_id": user_id,
                "portal_id": portal_id,
                "identification_rule_id": 0,
                "qrcode_value": "",
                "uhf_tag": "",
                "pin_value": "",
                "card_value": 0,
                "confidence": 1500 if user_id else 0,
                "mask": 0,
                "log_type_id": 0,
            }
            if extra:
                log.update(extra)
            estado.tabelas["access_logs"].append(log)
            if user_id:
                for u in estado.tabelas["users"]:
                    if u["id"] == user_id:
                        u["last_access"] = log["time"]
        _notificar("dao", {"object_changes": [{"object": "access_logs", "type": "inserted", "values": {k: v for k, v in log.items()}}], "device_id": estado.device_id})
        return log

    def _notificar(endpoint, payload):
        url = estado.monitor_url(endpoint)
        if not url:
            return
        try:
            # [DOC] o Monitor manda POST JSON e espera HTTP OK
            requests.post(url, json=payload, timeout=float(estado.config["monitor"].get("request_timeout", "5000")) / 1000)
        except requests.RequestException as e:
            estado.monitor_falhas.append(f"{endpoint}: {e}")

    def _tem_regra_valida(user_id, portal_id=None):
        # [DOC] bloqueios primeiro; se nenhum bate, permissões. Simplificado: basta uma permissão
        # ligada ao usuário cujo time_span cubra o horário atual e cujo portal_access_rules inclua
        # o portal usado (regra sem portal vale em todos). Sem regra → negado.
        regras_ids = {r["access_rule_id"] for r in estado.tabelas["user_access_rules"] if r["user_id"] == user_id}
        if portal_id is not None:
            com_portal = {r["access_rule_id"] for r in estado.tabelas["portal_access_rules"]}
            neste = {r["access_rule_id"] for r in estado.tabelas["portal_access_rules"] if r["portal_id"] == portal_id}
            regras_ids = {r for r in regras_ids if r not in com_portal or r in neste}
        for g in estado.tabelas["user_groups"]:
            if g["user_id"] == user_id:
                regras_ids |= {r["access_rule_id"] for r in estado.tabelas["group_access_rules"] if r["group_id"] == g["group_id"]}
        if not regras_ids:
            return False
        agora = datetime.fromtimestamp(estado.agora(), tz=timezone.utc)
        seg = agora.hour * 3600 + agora.minute * 60 + agora.second
        dia = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[agora.weekday()]
        for rid in regras_ids:
            regra = next((r for r in estado.tabelas["access_rules"] if r["id"] == rid), None)
            if not regra or regra.get("type") != 1:
                continue
            tzs = [t["time_zone_id"] for t in estado.tabelas["access_rule_time_zones"] if t["access_rule_id"] == rid]
            if not tzs:
                return True  # regra sem horário = sempre
            for span in estado.tabelas["time_spans"]:
                if span["time_zone_id"] in tzs and span.get(dia) and span["start"] <= seg <= span["end"]:
                    return True
        return False

    def identificar(user_id, component_id="0MS1", girar=True):
        """Simula um rosto reconhecido no leitor `component_id`. Devolve o log gravado."""
        portal = _portal_por_component(component_id)
        usuario = next((u for u in estado.tabelas["users"] if u["id"] == user_id), None)
        if not usuario or user_id not in estado.imagens:
            return _gravar_log(event=3, user_id=0, portal_id=portal)
        servidor = estado.servidor_online_url()
        if servidor and not estado.modo_contingencia:
            # [DOC] modo Pro: POST form-urlencoded em /new_user_identified.fcgi; resposta {"result": {...}}
            campos = {
                "device_id": estado.device_id, "identifier_id": 1, "time": estado.agora(), "portal_id": portal,
                "uuid": secrets.token_hex(8), "user_id": user_id, "user_name": usuario["name"], "event": 7,
                "duress": 0, "face_mask": 0, "confidence": 1500, "log_type_id": 0, "component_id": component_id,
                "user_has_image": 1,
            }
            try:
                r = requests.post(f"{servidor}/new_user_identified.fcgi", data=campos, timeout=5)
                r.raise_for_status()
                resultado = r.json().get("result", {})
            except (requests.RequestException, ValueError) as e:
                estado.monitor_falhas.append(f"online: {e}")
                estado.modo_contingencia = True
                _notificar("operation_mode", {"operation_mode": {"mode": 2, "mode_name": "contingency", "time": estado.agora(), "last_offline": estado.agora(), "exception_mode": 0}, "device_id": estado.device_id})
                resultado = None
            if resultado is not None:
                evento = int(resultado.get("event", 6))
                log = _gravar_log(event=evento, user_id=user_id, portal_id=portal)
                acoes = resultado.get("actions") or []
                liberou = any(a.get("action") == "catra" for a in acoes)
                if evento == 7 and liberou:
                    _registrar_giro(log, portal, girar)
                return log
        # Standalone / contingência: decide local
        evento = 7 if _tem_regra_valida(user_id, portal) else 6
        log = _gravar_log(event=evento, user_id=user_id, portal_id=portal)
        if evento == 7:
            _registrar_giro(log, portal, girar)
        return log

    def _registrar_giro(log, portal, girar):
        if girar:
            # [DOC] catra_event: TURN_RIGHT/TURN_LEFT; [SUP] portal 1 = giro à direita
            nome = "EVENT_TURN_RIGHT" if portal == 1 else "EVENT_TURN_LEFT"
            _notificar("catra_event", {"event": {"type": 7, "name": nome, "time": estado.agora(), "uuid": secrets.token_hex(8)}, "access_event_id": log["id"], "device_id": estado.device_id, "time": estado.agora()})
        else:
            # [DOC] sem giro dentro do tempo de liberação → evento 13 (desistência) no log
            _gravar_log(event=13, user_id=log["user_id"], portal_id=portal)
            _notificar("catra_event", {"event": {"type": 9, "name": "EVENT_GIVE_UP", "time": estado.agora(), "uuid": secrets.token_hex(8)}, "access_event_id": log["id"], "device_id": estado.device_id, "time": estado.agora()})

    app.identificar = identificar

    def alive():
        # [DOC] device_is_alive via Monitor: {access_logs: <n pendentes>, device_id, time}
        _notificar("device_is_alive", {"access_logs": len(estado.tabelas["access_logs"]), "device_id": estado.device_id, "time": estado.agora()})
        servidor = estado.servidor_online_url()
        if servidor and estado.modo_contingencia:
            try:
                r = requests.post(f"{servidor}/device_is_alive.fcgi", json={"access_logs": len(estado.tabelas["access_logs"]), "device_id": estado.device_id, "time": estado.agora()}, timeout=5)
                if r.ok:
                    estado.modo_contingencia = False
                    _notificar("operation_mode", {"operation_mode": {"mode": 1, "mode_name": "online", "time": estado.agora(), "last_offline": 0, "exception_mode": 0}, "device_id": estado.device_id})
            except requests.RequestException:
                pass

    app.alive = alive

    # ---------------------------------------------------------------- endpoints só do simulador
    @app.post("/sim/identificar")
    def sim_identificar():
        c = request.get_json(force=True)
        log = identificar(int(c["user_id"]), c.get("component_id", "0MS1"), bool(c.get("girar", True)))
        return jsonify(log)

    @app.post("/sim/alive")
    def sim_alive():
        alive()
        return jsonify({"contingencia": estado.modo_contingencia})

    @app.post("/sim/botoeira")
    def sim_botoeira():
        portal = int((request.get_json(silent=True) or {}).get("portal_id") or 1)
        log = _gravar_log(event=11, user_id=0, portal_id=portal)
        servidor = estado.servidor_online_url()
        if servidor and not estado.modo_contingencia:
            # [DOC] em modo online a botoeira também vai por POST JSON em /new_rex_log.fcgi (resposta vazia)
            rex = {"time": log["time"], "event": 11, "user_id": 0, "portal_id": portal}
            try:
                requests.post(f"{servidor}/new_rex_log.fcgi", json={"device_id": estado.device_id, "rex_log": rex}, timeout=5)
            except requests.RequestException as e:
                estado.monitor_falhas.append(f"rex: {e}")
        return jsonify(log)

    @app.get("/sim/estado")
    def sim_estado():
        return jsonify({
            "device_id": estado.device_id,
            "usuarios": len(estado.tabelas["users"]),
            "fotos": len(estado.imagens),
            "logs": len(estado.tabelas["access_logs"]),
            "config": estado.config,
            "liberacoes": estado.liberacoes[-10:],
            "contingencia": estado.modo_contingencia,
            "monitor_falhas": estado.monitor_falhas[-10:],
        })

    return app


def main():
    p = argparse.ArgumentParser(description="Simulador de catraca Control iD")
    p.add_argument("--porta", type=int, default=8085)
    p.add_argument("--device-id", type=int, default=935107)
    p.add_argument("--serial", default="SIM-0001")
    a = p.parse_args()
    app = criar_simulador(EstadoCatraca(device_id=a.device_id, serial=a.serial))
    print(f"catraca simulada em http://127.0.0.1:{a.porta}  (device_id={a.device_id})")
    app.run(host="127.0.0.1", port=a.porta, debug=False)


if __name__ == "__main__":
    main()
