"""Funções puras de uso geral (datas, fuso, formatação)."""
from datetime import datetime, timezone, date, time
from zoneinfo import ZoneInfo

FUSO_PADRAO = "America/Sao_Paulo"


def agora_utc():
    return datetime.now(timezone.utc)


def _zona(fuso):
    try:
        return ZoneInfo(fuso or FUSO_PADRAO)
    except Exception:  # noqa: BLE001 — fuso inválido no banco não pode derrubar a tela
        return ZoneInfo(FUSO_PADRAO)


def para_local(dt, fuso=FUSO_PADRAO):
    """timestamptz (UTC) → datetime no fuso da unidade."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_zona(fuso))


def de_unix(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def para_unix(dt):
    return int(dt.timestamp())


def formatar_hora(dt, fuso=FUSO_PADRAO):
    dt = para_local(dt, fuso)
    return dt.strftime("%H:%M") if dt else ""


def formatar_data_hora(dt, fuso=FUSO_PADRAO):
    dt = para_local(dt, fuso)
    return dt.strftime("%d/%m/%Y %H:%M") if dt else ""


def combinar_local(dia: date, hora: time, fuso=FUSO_PADRAO):
    """Data + hora da turma (no fuso da escola) → datetime com fuso."""
    return datetime.combine(dia, hora, tzinfo=_zona(fuso))


def primeiro_nome(nome):
    return (nome or "").strip().split(" ")[0]
