"""Adaptador Control iD contra o simulador, por HTTP real (requests ↔ Flask)."""
from datetime import datetime, timezone

import pytest

from integrations.base_adapter import DispositivoErro, FotoRejeitada
from integrations.controlid.adapter import ControlIdAdapter
from tests.conftest import JPEG_OK, JPEG_OUTRO, JPEG_SEM_ROSTO


@pytest.fixture
def adapter(simulador):
    return ControlIdAdapter("127.0.0.1", simulador["porta"], "admin", "admin")


def test_login_e_identificacao(adapter, simulador):
    sessao = adapter.connect()
    assert sessao and adapter.health()
    info = adapter.identify_device()
    assert info["device_id"] == str(simulador["estado"].device_id)
    assert info["serial"] == "SIM-0001"


def test_senha_errada(simulador):
    a = ControlIdAdapter("127.0.0.1", simulador["porta"], "admin", "errada")
    with pytest.raises(DispositivoErro):
        a.connect()


def test_sessao_expirada_reloga_sozinho(adapter, simulador):
    adapter.connect()
    simulador["estado"].sessoes.clear()  # catraca "esqueceu" a sessão
    assert adapter.status()["serial"] == "SIM-0001"


def test_sync_user_cria_depois_modifica_e_reconcilia_por_registration(adapter, simulador):
    uid = adapter.sync_user({"id": 42, "nome": "Maria Silva"})
    assert uid == 1
    assert adapter.sync_user({"id": 42, "nome": "Maria S. Silva", "user_id_no_dispositivo": uid}) == uid
    users = adapter.load_objects("users")
    assert len(users) == 1 and users[0]["name"] == "Maria S. Silva" and users[0]["registration"] == "42"
    # perdeu o user_id (ex.: banco restaurado): acha pelo registration em vez de duplicar
    assert adapter.sync_user({"id": 42, "nome": "Maria"}) == uid
    assert len(adapter.load_objects("users")) == 1


def test_foto_ok_rejeitada_e_duplicada(adapter):
    uid = adapter.sync_user({"id": 1, "nome": "A"})
    r = adapter.set_user_photo(uid, JPEG_OK)
    assert r["success"] and "scores" in r
    assert adapter.list_user_images() == [uid]
    assert adapter.get_user_photo(uid) == JPEG_OK
    with pytest.raises(FotoRejeitada) as e:
        adapter.set_user_photo(uid, JPEG_SEM_ROSTO)
    assert e.value.erros[0]["code"] == 2
    with pytest.raises(FotoRejeitada):
        adapter.set_user_photo(uid, b"nao e jpeg")
    uid2 = adapter.sync_user({"id": 2, "nome": "B"})
    with pytest.raises(FotoRejeitada) as e:  # match=1: mesma face em outro usuário
        adapter.set_user_photo(uid2, JPEG_OK)
    assert e.value.erros[0]["code"] == 3
    adapter.set_user_photo(uid2, JPEG_OUTRO)


def test_regras_de_acesso_e_revogacao(adapter, simulador):
    uid = adapter.sync_user({"id": 1, "nome": "A"})
    regra = adapter.criar_regra_horario("Liberado", 0, 86399)
    adapter.set_access_rules(uid, [regra])
    assert adapter.load_objects("user_access_rules") == [{"user_id": uid, "access_rule_id": regra}]
    assert len(adapter.load_objects("portal_access_rules")) == 2  # portais de fábrica
    adapter.set_access_rules(uid, [regra])  # idempotente (destrói e recria)
    assert len(adapter.load_objects("user_access_rules")) == 1
    adapter.revoke(uid)
    assert adapter.load_objects("user_access_rules") == []


def test_remove_user_apaga_foto(adapter):
    uid = adapter.sync_user({"id": 1, "nome": "A"})
    adapter.set_user_photo(uid, JPEG_OK)
    adapter.remove_user(uid)
    assert adapter.load_objects("users") == [] and adapter.list_user_images() == []


def test_fetch_logs_incremental(adapter, simulador):
    sim = simulador["app"]
    uid = adapter.sync_user({"id": 1, "nome": "A"})
    adapter.set_user_photo(uid, JPEG_OK)
    adapter.set_access_rules(uid, [adapter.criar_regra_horario("L", 0, 86399)])
    sim.identificar(uid, "0MS1")           # evento 7 (liberado)
    sim.identificar(999, "0MS1")           # evento 3 (não identificado)
    logs = adapter.fetch_logs(0)
    assert [l["event"] for l in logs] == [7, 3]
    assert adapter.fetch_logs(logs[-1]["id"]) == []
    assert [l["id"] for l in adapter.fetch_logs(1)] == [2]


def test_release_gera_evento_10(adapter, simulador):
    adapter.release("entrada")
    assert simulador["estado"].liberacoes[-1]["parameters"] == "allow=clockwise"
    assert adapter.fetch_logs(0)[-1]["event"] == 10


def test_set_clock(adapter, simulador):
    adapter.set_clock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), fuso="UTC")
    assert abs(simulador["estado"].agora() - int(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp())) < 5


def test_configurar_monitor_e_modo_online(adapter, simulador):
    adapter.configurar_monitor("127.0.0.1", 5000, "api/notifications/tok")
    assert simulador["estado"].config["monitor"]["hostname"] == "127.0.0.1"
    assert simulador["estado"].monitor_url("dao") == "http://127.0.0.1:5000/api/notifications/tok/dao"
    sid = adapter.configurar_modo_online("http://127.0.0.1:5000/api/catraca/tok")
    assert simulador["estado"].servidor_online_url() == "http://127.0.0.1:5000/api/catraca/tok"
    assert adapter.configurar_modo_online("http://127.0.0.1:5000/api/catraca/tok") == sid  # não duplica o device
    adapter.configurar_modo_online("x", ativar=False)
    assert simulador["estado"].servidor_online_url() is None
