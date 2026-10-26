"""Adaptador Control iD (iDFace / idBlock Next) — API REST documentada em
https://www.controlid.com.br/docs/access-api-pt/

Cada método diz de onde veio. Legenda nos comentários:
  [DOC]   comportamento documentado pela Control iD
  [SUP]   suposição nossa, ainda não confirmada
  [HW]    precisa de teste no equipamento real
Ver README.md ao lado pra tabela consolidada.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from ..base_adapter import BaseAdapter, DispositivoErro, FotoRejeitada

# [DOC] códigos de evento de access_logs (Lista de Objetos)
EVENTO_DISPOSITIVO_INVALIDO = 1
EVENTO_PARAMETROS_INVALIDOS = 2
EVENTO_NAO_IDENTIFICADO = 3
EVENTO_IDENTIFICACAO_PENDENTE = 4
EVENTO_TEMPO_ESGOTADO = 5
EVENTO_ACESSO_NEGADO = 6
EVENTO_ACESSO_LIBERADO = 7
EVENTO_ACESSO_PENDENTE = 8
EVENTO_NAO_ADMIN = 9
EVENTO_ACESSO_NAO_IDENTIFICADO = 10
EVENTO_BOTOEIRA = 11
EVENTO_INTERFACE_WEB = 12
EVENTO_DESISTENCIA = 13
EVENTO_SEM_RESPOSTA = 14
EVENTO_INTERFONE = 15

# [DOC] catra: allow=clockwise | anticlockwise | both. Qual lado é entrada depende de
# sec_box.catra_side_to_enter ("0" = horário é entrada) — [HW] confirmar na instalação.
DIRECAO_CATRA = {"entrada": "clockwise", "saida": "anticlockwise", "ambos": "both"}

# [DOC] component_id nos eventos online do idBlock Next
COMPONENT_IDFACE_PRIMARIO = "0MS1"
COMPONENT_IDFACE_SECUNDARIO = "0MS2"


class ControlIdAdapter(BaseAdapter):
    def __init__(self, host, porta=80, usuario="admin", senha="admin", timeout=8, https=False, http=None):
        self.base_url = f"{'https' if https else 'http'}://{host}:{porta}"
        self.usuario = usuario
        self.senha = senha
        self.timeout = timeout
        self._http = http or requests.Session()
        self._sessao = None

    # ------------------------------------------------------------ transporte

    def _url(self, endpoint):
        return f"{self.base_url}/{endpoint}"

    def _requisicao(self, endpoint, json=None, data=None, params=None, headers=None, method="POST", relogar=True):
        if self._sessao is None:
            self.connect()
        params = dict(params or {})
        params["session"] = self._sessao  # [DOC] sessão vai na query string em todas as chamadas
        try:
            r = self._http.request(method, self._url(endpoint), json=json, data=data, params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise DispositivoErro(f"sem resposta de {self.base_url}: {e}") from e
        # [SUP] sessão expirada responde 401; a doc não diz. Relogamos uma vez e repetimos.
        if r.status_code == 401 and relogar:
            self._sessao = None
            return self._requisicao(endpoint, json=json, data=data, params=params, headers=headers, method=method, relogar=False)
        if r.status_code >= 400:
            raise DispositivoErro(f"{endpoint} → HTTP {r.status_code}: {r.text[:200]}")
        return r

    def _json(self, endpoint, corpo=None, **kw):
        r = self._requisicao(endpoint, json=corpo if corpo is not None else {}, **kw)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError as e:
            raise DispositivoErro(f"{endpoint} respondeu algo que não é JSON: {r.text[:200]}") from e

    # ------------------------------------------------------------ sessão

    def connect(self):
        # [DOC] POST /login.fcgi {login, password} → {"session": "..."}
        try:
            r = self._http.post(self._url("login.fcgi"), json={"login": self.usuario, "password": self.senha}, timeout=self.timeout)
        except requests.RequestException as e:
            raise DispositivoErro(f"sem resposta de {self.base_url}: {e}") from e
        if r.status_code >= 400:
            raise DispositivoErro(f"login recusado (HTTP {r.status_code})")
        sessao = r.json().get("session")
        if not sessao:
            raise DispositivoErro("login sem campo session")
        self._sessao = sessao
        return sessao

    def health(self):
        # [DOC] POST /session_is_valid.fcgi?session=... → {"session_is_valid": true}
        try:
            if self._sessao is None:
                self.connect()
            r = self._http.post(self._url("session_is_valid.fcgi"), params={"session": self._sessao}, timeout=self.timeout)
            return bool(r.ok and r.json().get("session_is_valid"))
        except (requests.RequestException, ValueError, DispositivoErro):
            return False

    def logout(self):
        # [DOC] POST /logout.fcgi
        if self._sessao:
            try:
                self._requisicao("logout.fcgi", relogar=False)
            finally:
                self._sessao = None

    # ------------------------------------------------------------ sistema

    def status(self):
        # [DOC] POST /system_information.fcgi → uptime, time, memory, license, network, serial, version, device_id, online
        return self._json("system_information.fcgi")

    def identify_device(self):
        info = self.status()
        return {
            "serial": info.get("serial"),
            "firmware": info.get("version"),
            "device_id": info.get("device_id"),
            "online": info.get("online"),
            "hora_unix": info.get("time"),
        }

    def set_clock(self, dt=None, fuso="America/Sao_Paulo"):
        # [DOC] POST /set_system_time.fcgi {day, month, year, hour, minute, second} — hora local do equipamento
        dt = (dt or datetime.now(timezone.utc)).astimezone(ZoneInfo(fuso))
        self._json("set_system_time.fcgi", {"day": dt.day, "month": dt.month, "year": dt.year, "hour": dt.hour, "minute": dt.minute, "second": dt.second})

    def set_configuration(self, modulos: dict):
        # [DOC] POST /set_configuration.fcgi {"modulo": {"chave": "valor"}} — valores sempre string
        self._json("set_configuration.fcgi", modulos)

    def configurar_monitor(self, hostname, port, path="api/notifications", request_timeout_ms=5000, alive_interval_ms=30000):
        # [DOC] módulo monitor: hostname, port, path, request_timeout, alive_interval, enable_photo_upload, inform_access_event_id
        self.set_configuration({
            "monitor": {
                "hostname": str(hostname),
                "port": str(port),
                "path": path,
                "request_timeout": str(request_timeout_ms),
                "alive_interval": str(alive_interval_ms),
                "enable_photo_upload": "0",
                "inform_access_event_id": "1",
            }
        })

    def configurar_modo_online(self, url_servidor, ativar=True, tentativas=3):
        """Modo Pro: identifica local, autoriza no servidor.
        [DOC] 1) create_objects devices {name, ip, public_key:''} 2) online_client.server_id
        3) general.online=1, local_identification=1, ihm_enterprise_mode=0, online_client.extract_template=0
        O servidor precisa responder /session_is_valid.fcgi e /new_user_identified.fcgi.
        [HW] se o modo pode ser diferente por leitor no idBlock Next (primário/secundário)."""
        if not ativar:
            self.set_configuration({"general": {"online": "0", "local_identification": "1"}})
            return None
        existentes = self.load_objects("devices", where={"devices": {"ip": url_servidor}})
        if existentes:
            server_id = existentes[0]["id"]
        else:
            server_id = self.create_objects("devices", [{"name": "Nexosix", "ip": url_servidor, "public_key": ""}])[0]
        self.set_configuration({"online_client": {"server_id": str(server_id), "extract_template": "0", "max_request_attempts": str(tentativas)}})
        self.set_configuration({"general": {"online": "1", "local_identification": "1", "ihm_enterprise_mode": "0"}})
        return server_id

    # ------------------------------------------------------------ objetos genéricos

    def create_objects(self, objeto, valores):
        # [DOC] POST /create_objects.fcgi {object, values:[...]} → {"ids":[...]}
        return self._json("create_objects.fcgi", {"object": objeto, "values": valores}).get("ids", [])

    def load_objects(self, objeto, where=None, fields=None, order=None, limit=None, offset=None):
        # [DOC] POST /load_objects.fcgi {object, fields?, where?, order?, limit?, offset?} → {objeto: [...]}
        corpo = {"object": objeto}
        if fields:
            corpo["fields"] = fields
        if where:
            corpo["where"] = where
        if order:
            corpo["order"] = order
        if limit is not None:
            corpo["limit"] = limit
        if offset is not None:
            corpo["offset"] = offset
        return self._json("load_objects.fcgi", corpo).get(objeto, [])

    def modify_objects(self, objeto, valores, where):
        # [DOC] POST /modify_objects.fcgi {object, values:{...}, where} → {"changes": n}
        return self._json("modify_objects.fcgi", {"object": objeto, "values": valores, "where": where}).get("changes", 0)

    def destroy_objects(self, objeto, where):
        # [DOC] POST /destroy_objects.fcgi {object, where} → {"changes": n}
        return self._json("destroy_objects.fcgi", {"object": objeto, "where": where}).get("changes", 0)

    # ------------------------------------------------------------ usuários

    def sync_user(self, pessoa):
        """pessoa = {id, nome, user_id_no_dispositivo?, begin_time?, end_time?}
        `registration` recebe o id da pessoa no Nexosix: é o elo pra reconciliar se o
        user_id do equipamento se perder (troca de catraca, reset)."""
        valores = {"name": pessoa["nome"][:100], "registration": str(pessoa["id"])}
        if pessoa.get("begin_time") is not None:
            valores["begin_time"] = int(pessoa["begin_time"])
        if pessoa.get("end_time") is not None:
            valores["end_time"] = int(pessoa["end_time"])
        user_id = pessoa.get("user_id_no_dispositivo")
        if user_id:
            if self.modify_objects("users", valores, {"users": {"id": int(user_id)}}) > 0:
                return int(user_id)
        achados = self.load_objects("users", where={"users": {"registration": str(pessoa["id"])}}, fields=["id"])
        if achados:
            user_id = int(achados[0]["id"])
            self.modify_objects("users", valores, {"users": {"id": user_id}})
            return user_id
        ids = self.create_objects("users", [valores])
        if not ids:
            raise DispositivoErro("create_objects users não devolveu id")
        return int(ids[0])

    def remove_user(self, user_id):
        # [DOC] user_destroy_image apaga foto e registro facial; destroy_objects users apaga o cadastro.
        # [SUP] destruir o usuário já apaga a foto; chamamos os dois pra não deixar template órfão.
        try:
            self._json("user_destroy_image.fcgi", {"user_id": int(user_id)})
        except DispositivoErro:
            pass
        self.destroy_objects("user_access_rules", {"user_access_rules": {"user_id": int(user_id)}})
        self.destroy_objects("users", {"users": {"id": int(user_id)}})

    def set_user_photo(self, user_id, jpeg, match=True, timestamp=None):
        # [DOC] POST /user_set_image.fcgi?user_id&timestamp&match  Content-Type application/octet-stream, < 2 MB
        #       → {user_id, success, errors:[{code,message}], scores:{...}}
        if len(jpeg) >= 2 * 1024 * 1024:
            raise FotoRejeitada([{"code": 1, "message": "foto maior que 2 MB"}])
        params = {"user_id": int(user_id), "timestamp": int(timestamp or datetime.now(timezone.utc).timestamp()), "match": 1 if match else 0}
        r = self._requisicao("user_set_image.fcgi", data=jpeg, params=params, headers={"Content-Type": "application/octet-stream"})
        resposta = r.json() if r.content else {"success": True}
        if not resposta.get("success", False):
            raise FotoRejeitada(resposta.get("errors"), resposta.get("scores"))
        return resposta

    def test_user_photo(self, jpeg):
        # [DOC] POST /user_test_image.fcgi — mesmos critérios do cadastro, não grava nada
        r = self._requisicao("user_test_image.fcgi", data=jpeg, headers={"Content-Type": "application/octet-stream"})
        return r.json() if r.content else {"success": True}

    def get_user_photo(self, user_id):
        # [DOC] GET /user_get_image.fcgi?user_id → image/jpeg
        r = self._requisicao("user_get_image.fcgi", params={"user_id": int(user_id)}, method="GET")
        return r.content

    def list_user_images(self):
        # [DOC] /user_list_images.fcgi → {"user_ids":[...]}
        return self._json("user_list_images.fcgi").get("user_ids", [])

    # ------------------------------------------------------------ regras de acesso

    def set_access_rules(self, user_id, regras):
        # [DOC] user_access_rules {user_id, access_rule_id}; o par é único
        self.destroy_objects("user_access_rules", {"user_access_rules": {"user_id": int(user_id)}})
        if regras:
            self.create_objects("user_access_rules", [{"user_id": int(user_id), "access_rule_id": int(r)} for r in regras])

    def revoke(self, user_id):
        self.destroy_objects("user_access_rules", {"user_access_rules": {"user_id": int(user_id)}})
        self.destroy_objects("user_groups", {"user_groups": {"user_id": int(user_id)}})

    def criar_regra_horario(self, nome, inicio_seg, fim_seg, dias=(1, 2, 3, 4, 5), portais=None):
        """Cria access_rule + time_zone + time_span + vínculos. Devolve o id da regra.
        [DOC] time_spans: start/end em segundos desde a meia-noite; sun..sat/hol1..3 como 0/1."""
        regra_id = self.create_objects("access_rules", [{"name": nome, "type": 1, "priority": 0}])[0]
        tz_id = self.create_objects("time_zones", [{"name": nome}])[0]
        span = {"time_zone_id": tz_id, "start": int(inicio_seg), "end": int(fim_seg), "hol1": 0, "hol2": 0, "hol3": 0}
        for i, chave in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat")):
            span[chave] = 1 if i in dias else 0
        self.create_objects("time_spans", [span])
        self.create_objects("access_rule_time_zones", [{"access_rule_id": regra_id, "time_zone_id": tz_id}])
        if portais is None:
            portais = [p["id"] for p in self.load_objects("portals")]
        if portais:
            self.create_objects("portal_access_rules", [{"portal_id": int(p), "access_rule_id": regra_id} for p in portais])
        return regra_id

    # ------------------------------------------------------------ catraca e logs

    def release(self, direction="ambos"):
        # [DOC] POST /execute_actions.fcgi {"actions":[{"action":"catra","parameters":"allow=clockwise"}]}
        allow = DIRECAO_CATRA.get(direction, "both")
        self._json("execute_actions.fcgi", {"actions": [{"action": "catra", "parameters": f"allow={allow}"}]})

    def fetch_logs(self, since_id=0, limit=500):
        # [DOC] load_objects access_logs com where id > since_id. Guardamos o último id por dispositivo.
        return self.load_objects(
            "access_logs",
            where={"access_logs": {"id": {">": int(since_id)}}},
            order=["id", "ascending"],
            limit=limit,
        )
