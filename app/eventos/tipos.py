"""Tipos de evento do Nexosix (coluna eventos.tipo). Centralizado pra painel e relatórios
não dependerem de string solta."""

ENTRADA = "ENTRADA"
ENTRADA_ATRASADA = "ENTRADA_ATRASADA"
SAIDA = "SAIDA"                      # funcionário/visitante ou aluno sem regra
SAIDA_ALUNO = "SAIDA_ALUNO"          # aluno retirado por responsável (correlacao_id = quem levou)
SAIDA_AUTONOMA = "SAIDA_AUTONOMA"
SAIDA_ANTECIPADA = "SAIDA_ANTECIPADA"  # liberação manual da recepção com justificativa
SAIDA_NEGADA = "SAIDA_NEGADA"
ACESSO_NEGADO = "ACESSO_NEGADO"
NAO_IDENTIFICADO = "NAO_IDENTIFICADO"
DESISTENCIA = "DESISTENCIA"          # liberou e não girou (evento 13 da Control iD)
RESPONSAVEL_CHEGOU = "RESPONSAVEL_CHEGOU"
LIBERACAO_MANUAL = "LIBERACAO_MANUAL"
BOTOEIRA = "BOTOEIRA"
DISPOSITIVO_ONLINE = "DISPOSITIVO_ONLINE"
DISPOSITIVO_OFFLINE = "DISPOSITIVO_OFFLINE"
DISPOSITIVO_CONTINGENCIA = "DISPOSITIVO_CONTINGENCIA"
FAILOVER_NUVEM = "FAILOVER_NUVEM"       # nuvem assumiu a unidade
FAILOVER_FIM = "FAILOVER_FIM"           # servidor local voltou e reassumiu
DISPOSITIVO_RESET = "DISPOSITIVO_RESET"  # catraca recomeçou a numeração (reset/troca)

ENTRADAS = {ENTRADA, ENTRADA_ATRASADA}
SAIDAS = {SAIDA, SAIDA_ALUNO, SAIDA_AUTONOMA, SAIDA_ANTECIPADA}
NEGADOS = {SAIDA_NEGADA, ACESSO_NEGADO, NAO_IDENTIFICADO}
# Tipos que avisaram os pais e precisam de correção se a pessoa desistir na catraca
AVISA_SAIDA_ENTRADA = ENTRADAS | SAIDAS

ROTULOS = {
    ENTRADA: "Entrada",
    ENTRADA_ATRASADA: "Entrada atrasada",
    SAIDA: "Saída",
    SAIDA_ALUNO: "Saída com responsável",
    SAIDA_AUTONOMA: "Saída autônoma",
    SAIDA_ANTECIPADA: "Saída antecipada (manual)",
    SAIDA_NEGADA: "Saída negada",
    ACESSO_NEGADO: "Acesso negado",
    NAO_IDENTIFICADO: "Não identificado",
    DESISTENCIA: "Desistência (não girou)",
    RESPONSAVEL_CHEGOU: "Responsável chegou",
    LIBERACAO_MANUAL: "Liberação manual",
    BOTOEIRA: "Botoeira",
    DISPOSITIVO_ONLINE: "Catraca online",
    DISPOSITIVO_OFFLINE: "Catraca offline",
    DISPOSITIVO_CONTINGENCIA: "Catraca em contingência",
    FAILOVER_NUVEM: "Nuvem assumiu (servidor local parou)",
    FAILOVER_FIM: "Servidor local reassumiu",
    DISPOSITIVO_RESET: "Catraca resetada (cadastro conferido)",
}
