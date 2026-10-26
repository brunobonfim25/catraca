"""Interface universal dos adaptadores de hardware.

Só entra aqui o que o equipamento suporta de verdade. Cada método recebe/devolve tipos
simples (dict, int, bytes) pra que o worker e os testes não dependam do fabricante.
"""
from abc import ABC, abstractmethod


class DispositivoErro(Exception):
    """Falha de comunicação ou resposta inesperada do equipamento."""


class FotoRejeitada(DispositivoErro):
    """O equipamento recusou a foto (sem rosto, desfocada, já cadastrada…).
    `erros` é a lista [{code, message}] do fabricante; `scores` as métricas, quando vierem."""

    def __init__(self, erros, scores=None):
        self.erros = erros or []
        self.scores = scores or {}
        msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in self.erros) or "foto rejeitada"
        super().__init__(msg)


class BaseAdapter(ABC):
    @abstractmethod
    def connect(self):
        """Abre sessão. Levanta DispositivoErro se não conseguir."""

    @abstractmethod
    def status(self) -> dict:
        """Informações do sistema (serial, firmware, hora, rede, online)."""

    @abstractmethod
    def identify_device(self) -> dict:
        """{serial, firmware, device_id} — pra conferir que o IP é a catraca certa."""

    @abstractmethod
    def sync_user(self, pessoa: dict) -> int:
        """Cria ou atualiza o usuário no equipamento. Devolve o id lá dentro."""

    @abstractmethod
    def remove_user(self, user_id: int) -> None:
        """Apaga usuário, foto e vínculos."""

    @abstractmethod
    def set_user_photo(self, user_id: int, jpeg: bytes) -> dict:
        """Envia a foto; o equipamento gera o template. Levanta FotoRejeitada."""

    @abstractmethod
    def set_access_rules(self, user_id: int, regras: list) -> None:
        """Substitui as regras de acesso do usuário (lista de ids de access_rules)."""

    @abstractmethod
    def revoke(self, user_id: int) -> None:
        """Tira todas as regras do usuário sem apagar o cadastro."""

    @abstractmethod
    def release(self, direction: str) -> None:
        """Libera a catraca: 'entrada' | 'saida' | 'ambos'."""

    @abstractmethod
    def fetch_logs(self, since_id: int, limit: int = 500) -> list:
        """Logs de acesso com id > since_id, em ordem crescente."""

    @abstractmethod
    def set_clock(self, dt) -> None:
        """Acerta o relógio do equipamento."""

    @abstractmethod
    def health(self) -> bool:
        """True se responde e a sessão vale."""
