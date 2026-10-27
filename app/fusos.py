"""Fusos horários aceitos nas unidades. Campo livre já quebrou o painel (alguém digitou a
cidade); agora é lista fechada e validada."""
from zoneinfo import available_timezones

FUSOS_BRASIL = [
    ("America/Sao_Paulo", "Brasília (SP, RJ, MG, Sul, BA, GO, DF, ES…)"),
    ("America/Manaus", "Manaus (AM, MT, MS, RO, RR)"),
    ("America/Cuiaba", "Cuiabá (MT)"),
    ("America/Campo_Grande", "Campo Grande (MS)"),
    ("America/Belem", "Belém (PA, AP)"),
    ("America/Fortaleza", "Fortaleza (CE, MA, PI, RN, PB)"),
    ("America/Recife", "Recife (PE)"),
    ("America/Maceio", "Maceió (AL, SE)"),
    ("America/Bahia", "Salvador (BA)"),
    ("America/Araguaina", "Araguaína (TO)"),
    ("America/Porto_Velho", "Porto Velho (RO)"),
    ("America/Boa_Vista", "Boa Vista (RR)"),
    ("America/Rio_Branco", "Rio Branco (AC)"),
    ("America/Noronha", "Fernando de Noronha"),
]
PADRAO = "America/Sao_Paulo"


def validar(fuso):
    """Devolve o fuso se for válido; senão o padrão (nunca deixa valor inválido chegar ao SQL)."""
    fuso = (fuso or "").strip()
    return fuso if fuso in available_timezones() else PADRAO
