<#
Para (atualização) ou remove (desinstalação) o servidor da escola.

  -SoParar      antes de atualizar: sobe para a nuvem o que ainda não subiu e para os serviços
  (sem opção)   desinstalar: também tira os serviços, a regra do firewall e o backup agendado
  -ApagarDados  apaga o banco local (fotos = dado biométrico), os backups e o .env.
                Sem isso, instalar de novo neste PC continua de onde parou, sem pedir código.
#>
param([switch]$SoParar, [switch]$ApagarDados)
. (Join-Path $PSScriptRoot 'comum.ps1')
$script:EtapaLog = if ($SoParar) { 'parar' } else { 'remover' }

try {
    # Eventos da catraca que ainda não subiram (de manhã, por exemplo) não podem ficar só neste PC
    if ((Test-Path $PY) -and (Test-Path $ENV_ARQ) -and (ServicoExiste $SVC_BANCO)) {
        Start-Service -Name $SVC_BANCO -ErrorAction SilentlyContinue
        EsperarServico $SVC_BANCO 'Running' 30 | Out-Null
        Log 'enviando para a nuvem o que ainda não subiu'
        Rodar $PY @('-m', 'flask', '--app', 'wsgi', 'espelho-enviar') -Dir $APP -PodeFalhar -Ambiente ((LerEnv) + @{ PYTHONIOENCODING = 'utf-8'; PYTHONUTF8 = '1' }) | Out-Null
    }
    foreach ($n in $SVC_AGENTE, $SVC_WORKER, $SVC_BANCO) { PararServico $n }
    if ($SoParar) { Log 'serviços parados'; exit 0 }

    foreach ($n in $SVC_AGENTE, $SVC_WORKER) {
        $exe = Join-Path $SERVICOS "$n.exe"
        if ((ServicoExiste $n) -and (Test-Path $exe)) { Rodar $exe @('uninstall') -PodeFalhar | Out-Null }
        if (ServicoExiste $n) { Rodar 'sc.exe' @('delete', $n) -PodeFalhar | Out-Null }
        EsperarServico $n 'Ausente' 30 | Out-Null
        Remove-Item (Join-Path $SERVICOS "$n.*") -Force -ErrorAction SilentlyContinue
    }
    if (ServicoExiste $SVC_BANCO) {
        Rodar (Join-Path $PGBIN 'pg_ctl.exe') @('unregister', '-N', $SVC_BANCO) -PodeFalhar | Out-Null
        if (ServicoExiste $SVC_BANCO) { Rodar 'sc.exe' @('delete', $SVC_BANCO) -PodeFalhar | Out-Null }
        EsperarServico $SVC_BANCO 'Ausente' 30 | Out-Null
    }
    Get-NetFirewallRule -DisplayName $REGRA_FIREWALL -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Unregister-ScheduledTask -TaskName $TAREFA_BACKUP -Confirm:$false -ErrorAction SilentlyContinue
    Log 'serviços, firewall e backup agendado removidos'

    if ($ApagarDados) {
        Remove-Item (Join-Path $RAIZ 'dados'), $BACKUPS, $ENV_ARQ, (Join-Path $LOGS 'escola.txt') -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path (Join-Path $RAIZ 'dados')) { throw 'não consegui apagar a pasta de dados (algum programa está usando)' }
        Log 'banco local, backups e configuração apagados'
        # os logs do worker citam nomes de alunos: saem junto (é o último passo, o log some com eles)
        Remove-Item $LOGS -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Log "dados mantidos em $RAIZ\dados (instalar de novo neste PC continua de onde parou)"
    }
    exit 0
} catch {
    Log "ERRO: $($_.Exception.Message)"
    GravarErro $_.Exception.Message
    exit 1
}
