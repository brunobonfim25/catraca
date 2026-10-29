<#
Teste ponta a ponta do instalador num Windows de verdade (GitHub Actions, windows-2025).
Sobe uma nuvem de teste neste PC (o próprio sistema, MODO=nuvem, porta 8099) e então:

  1. código errado      → instalador sai com código 10, nada fica rodando; desinstala
  2. instalação         → serviços, site na porta 80, catraca falando com o agente, cadastro
                          baixado da nuvem, nuvem vê o agente, .env trancado, backup, religar sozinho
  3. atualização        → roda de novo sem código: mesma configuração e mesmos dados
  4. desinstalar mantendo dados → reinstalar sem código continua de onde parou
  5. desinstalar apagando dados → nada sobra (pasta, serviços, firewall, tarefa, porta)

  .\deploy\windows\agente\testar.ps1 -Instalador dist\NexosixAgente-Setup.exe -Montado build\agente
#>
param(
    [Parameter(Mandatory = $true)][string]$Instalador,
    [Parameter(Mandatory = $true)][string]$Montado,
    [string]$Saida = 'build\teste'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$REPO = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$Instalador = (Resolve-Path $Instalador).Path
$Montado = (Resolve-Path $Montado).Path
New-Item -ItemType Directory -Force -Path $Saida | Out-Null
$Saida = (Resolve-Path $Saida).Path
$RAIZ = Join-Path $env:SystemDrive 'nexosix'
$NUVEM = 'http://127.0.0.1:8099'
$PGBIN = Join-Path $Montado 'pgsql\bin'
$script:falhas = 0

function Passo($t) { Write-Host "`n==================== $t" -ForegroundColor Cyan }
function Ok($cond, [string]$msg) {
    if ($cond) { Write-Host "  ok: $msg" -ForegroundColor Green }
    else { Write-Host "  FALHOU: $msg" -ForegroundColor Red; $script:falhas++; throw "FALHOU: $msg" }
}
function Nativo([string]$Exe, [string[]]$Argumentos, [switch]$Saida) {
    $antes = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = & $Exe @Argumentos 2>&1 | ForEach-Object { "$_" }; $c = $LASTEXITCODE } finally { $ErrorActionPreference = $antes }
    if (-not $Saida) { $out | ForEach-Object { Write-Host "    $_" } }
    if ($c -ne 0) { throw "$Exe $($Argumentos -join ' ') terminou com código ${c}: $($out -join ' | ')" }
    if ($Saida) { return ($out | Where-Object { $_ -and $_ -notmatch '^\s*$' } | Select-Object -Last 1) }
}
function Esperar([scriptblock]$Cond, [int]$Segundos, [string]$Msg) {
    $fim = (Get-Date).AddSeconds($Segundos)
    while ((Get-Date) -lt $fim) { try { if (& $Cond) { Ok $true $Msg; return } } catch { }; Start-Sleep -Seconds 2 }
    Ok $false "$Msg (esperei $Segundos s)"
}
function Http([string]$Url, [string]$Metodo = 'GET') {
    try { return Invoke-WebRequest $Url -Method $Metodo -UseBasicParsing -TimeoutSec 10 } catch { if ($_.Exception.Response) { return $_.Exception.Response } ; throw }
}
function JanelasAbertas { Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object { "$($_.ProcessName) [$($_.Id)]: $($_.MainWindowTitle)" } }
function EsperarProcesso($p, [int]$Minutos, [string]$Nome) {
    # Vigia: um diálogo escondido (o que /SUPPRESSMSGBOXES não cobre) prenderia o teste até o limite do job
    if (-not $p.WaitForExit($Minutos * 60000)) {
        $j = (JanelasAbertas) -join ' || '
        Get-Process | Where-Object { $_.ProcessName -like 'NexosixAgente-Setup*' -or $_.ProcessName -like '_iu*' -or $_.ProcessName -like 'unins*' } | Stop-Process -Force -ErrorAction SilentlyContinue
        throw "$Nome não terminou em $Minutos min. Janelas abertas: $j"
    }
}
function Setup([string[]]$Argumentos, [string]$Log) {
    $todos = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$Saida\$Log`"") + $Argumentos
    $p = Start-Process -FilePath $Instalador -ArgumentList $todos -PassThru
    $null = $p.Handle   # PowerShell 5.1: sem pegar o handle cedo, o ExitCode vem vazio
    EsperarProcesso $p 15 'o instalador'
    Write-Host "  instalador saiu com código $($p.ExitCode)"
    return $p.ExitCode
}
function Desinstalar([switch]$ApagarDados, [string]$Log) {
    $unins = Join-Path $RAIZ 'unins000.exe'
    Ok (Test-Path $unins) 'desinstalador existe'
    $a = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$Saida\$Log`"")
    if ($ApagarDados) { $a += '/APAGARDADOS' }
    $p = Start-Process -FilePath $unins -ArgumentList $a -PassThru
    EsperarProcesso $p 10 'o desinstalador'
    # o desinstalador do Inno se copia para a pasta temporária e o original sai na hora
    Esperar { -not (Test-Path $unins) -and -not (Get-Process | Where-Object { $_.ProcessName -like '_iu*' }) } 300 'desinstalador terminou'
}
function GuardarLogs([string]$Fase) {
    $d = Join-Path $Saida "logs-$Fase"
    if (Test-Path "$RAIZ\logs") { Copy-Item "$RAIZ\logs" $d -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$RAIZ\dados\pg\log") { Copy-Item "$RAIZ\dados\pg\log" "$d\pg" -Recurse -Force -ErrorAction SilentlyContinue }
}
function SidsComAcesso([string]$Caminho) {
    (Get-Acl $Caminho).Access | ForEach-Object {
        try { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { "$($_.IdentityReference)" }
    }
}
$script:AmbNuvem = @{}
function Nuvem([scriptblock]$Corpo) {
    # Só os comandos da nuvem de teste veem estas variáveis: o instalador herdaria MODO=nuvem e o
    # DATABASE_URL da nuvem e migraria o banco errado
    foreach ($k in $script:AmbNuvem.Keys) { [Environment]::SetEnvironmentVariable($k, $script:AmbNuvem[$k]) }
    try { & $Corpo } finally { foreach ($k in $script:AmbNuvem.Keys) { [Environment]::SetEnvironmentVariable($k, $null) } }
}
function ServicosRodando { foreach ($n in 'NexosixBanco', 'NexosixAgente', 'NexosixWorker') { if ("$((Get-Service $n -ErrorAction SilentlyContinue).Status)" -ne 'Running') { return $false } }; return $true }
function NenhumServico { -not (Get-Service 'NexosixBanco', 'NexosixAgente', 'NexosixWorker' -ErrorAction SilentlyContinue) }
function CargaFeita {
    $env:PGPASSWORD = ((Get-Content "$RAIZ\app\.env" | Where-Object { $_ -match '^DATABASE_URL=' }) -replace '^DATABASE_URL=postgresql://postgres:([^@]+)@.*$', '$1')
    try { $r = Nativo (Join-Path $RAIZ 'pgsql\bin\psql.exe') @('-w', '-h', '127.0.0.1', '-p', '5439', '-U', 'postgres', '-d', 'nexosix', '-tAc', 'SELECT carga_em IS NOT NULL FROM espelho_estado WHERE id = 1') -Saida } finally { Remove-Item Env:\PGPASSWORD }
    return $r -eq 't'
}
function ConferirInstalado([string]$Fase) {
    Ok (ServicosRodando) "($Fase) serviços NexosixBanco, NexosixAgente e NexosixWorker rodando"
    foreach ($n in 'NexosixBanco', 'NexosixAgente', 'NexosixWorker') {
        Ok ("$((Get-Service $n).StartType)" -eq 'Automatic') "($Fase) $n inicia com o Windows"
    }
    Ok ((Http 'http://127.0.0.1/health').StatusCode -eq 200) "($Fase) site do servidor responde na porta 80"
    $ag = Http 'http://127.0.0.1/agente'
    Ok ($ag.StatusCode -eq 200 -and $ag.Content -match 'Escola Teste') "($Fase) página de status mostra a escola"
    Ok ((Http 'http://127.0.0.1/api/catraca/tok-ci/session_is_valid.fcgi' 'POST').StatusCode -eq 200) "($Fase) catraca autenticada pelo token da escola fala com o servidor"
    Ok ([int](Http 'http://127.0.0.1/api/catraca/token-errado/session_is_valid.fcgi' 'POST').StatusCode -eq 403) "($Fase) token errado é recusado"
    Esperar { CargaFeita } 150 "($Fase) cadastro da escola baixado da nuvem"
    $pgEscuta = Get-NetTCPConnection -LocalPort 5439 -State Listen
    Ok (-not ($pgEscuta | Where-Object { $_.LocalAddress -notin '127.0.0.1' })) "($Fase) banco local só escuta em 127.0.0.1"
    $proibidos = 'S-1-5-32-545', 'S-1-5-11', 'S-1-1-0'   # Usuários, Usuários autenticados, Todos
    $sids = SidsComAcesso "$RAIZ\app\.env"
    Ok (-not ($sids | Where-Object { $_ -in $proibidos })) "($Fase) .env só para SYSTEM e Administradores ($($sids -join ', '))"
    Ok ([bool](Get-NetFirewallRule -DisplayName 'Nexosix - servidor da escola' -ErrorAction SilentlyContinue)) "($Fase) regra do firewall criada"
    Ok ([bool](Get-ScheduledTask -TaskName 'NexosixBackup' -ErrorAction SilentlyContinue)) "($Fase) backup diário agendado"
}

try {
    Passo 'Nuvem de teste'
    $pgNuvem = Join-Path $env:RUNNER_TEMP 'nuvem-pg'
    if (-not $env:RUNNER_TEMP) { $pgNuvem = Join-Path $env:TEMP 'nuvem-pg' }
    Nativo (Join-Path $PGBIN 'initdb.exe') @('-D', $pgNuvem, '-U', 'postgres', '-A', 'trust', '-E', 'UTF8', '--locale=C')
    # Start-Process, não Nativo: o postgres herda a saída do pg_ctl e quem lê essa saída esperaria
    # para sempre (foi o que travou a primeira rodada deste teste)
    Start-Process -FilePath (Join-Path $PGBIN 'pg_ctl.exe') -WindowStyle Hidden `
        -ArgumentList @('-D', "`"$pgNuvem`"", '-o', '"-p 5433 -c listen_addresses=127.0.0.1"', '-l', "`"$Saida\nuvem-pg.log`"", 'start') | Out-Null
    Esperar { & (Join-Path $PGBIN 'pg_isready.exe') -h 127.0.0.1 -p 5433 | Out-Null; $LASTEXITCODE -eq 0 } 60 'banco da nuvem de teste no ar'
    Nativo (Join-Path $PGBIN 'psql.exe') @('-w', '-h', '127.0.0.1', '-p', '5433', '-U', 'postgres', '-c', 'CREATE DATABASE nuvem')
    $venv = Join-Path $env:TEMP 'venv-nuvem'
    Nativo 'python' @('-m', 'venv', $venv)
    $vpy = Join-Path $venv 'Scripts\python.exe'
    Nativo $vpy @('-m', 'pip', 'install', '-q', '--disable-pip-version-check', '-r', (Join-Path $REPO 'requirements.txt'))
    $script:AmbNuvem = @{
        MODO = 'nuvem'; DATABASE_URL = 'postgresql://postgres@127.0.0.1:5433/nuvem'; BASE_URL = $NUVEM; PORT = '8099'; PYTHONIOENCODING = 'utf-8'
        SECRET_KEY = -join ((48..57 + 65..90 + 97..122) | Get-Random -Count 60 | ForEach-Object { [char]$_ })
    }
    $ci = Join-Path $PSScriptRoot 'ci_nuvem.py'
    Nuvem { Nativo $vpy @($ci, 'semear') }
    $nuvemProc = Nuvem { Start-Process -FilePath $vpy -ArgumentList 'serve.py' -WorkingDirectory $REPO -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "$Saida\nuvem.out.log" -RedirectStandardError "$Saida\nuvem.err.log" }
    Esperar { (Http "$NUVEM/health").StatusCode -eq 200 } 60 'nuvem de teste no ar'

    Passo 'Porta 80 do Windows de teste'
    # A imagem do GitHub vem com o IIS ocupando a porta 80 (o instalador recusa, com razão: a
    # catraca não chegaria no servidor). Num PC de escola isso não existe; aqui desligamos.
    foreach ($s in 'W3SVC', 'WAS', 'WMSVC') { Stop-Service -Name $s -Force -ErrorAction SilentlyContinue; Set-Service -Name $s -StartupType Disabled -ErrorAction SilentlyContinue }
    $uso = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue
    if ($uso) { Write-Host (netsh http show servicestate | Out-String) }
    Ok (-not $uso) 'porta 80 livre para o teste'

    Passo '1. Código errado'
    $c = Setup @('/CODIGO=AAAA-AAAA', "/NUVEM=$NUVEM") 'setup-codigo-errado.log'
    Ok ($c -eq 10) 'instalador sai com código 10'
    $erro = Get-Content "$RAIZ\logs\instalacao-erro.txt" -ErrorAction SilentlyContinue
    Ok ("$erro" -match 'inválido ou vencido') "mensagem clara: $erro"
    Ok (NenhumServico) 'nenhum serviço ficou registrado'
    Ok (-not (Test-Path "$RAIZ\app\.env")) 'nenhuma configuração ficou para trás'
    Ok ((Setup @() 'setup-silencioso-sem-codigo.log') -ne 0) 'silencioso sem código e sem instalação anterior: recusa'
    Ok (NenhumServico) 'e não registra nada'
    GuardarLogs '1-codigo-errado'
    Desinstalar -ApagarDados 'desinstalar-1.log'
    Ok (-not (Test-Path $RAIZ)) 'pasta removida'

    Passo '2. Instalação'
    $codigo = Nuvem { Nativo $vpy @($ci, 'codigo') -Saida }
    Write-Host "  código: $codigo"
    $t0 = Get-Date
    Ok ((Setup @("/CODIGO=$codigo", "/NUVEM=$NUVEM") 'setup-instalar.log') -eq 0) 'instalador sai com código 0'
    Write-Host "  levou $([int]((Get-Date) - $t0).TotalSeconds) s"
    ConferirInstalado 'instalação'
    Esperar { Nuvem { Nativo $vpy @($ci, 'visto') -Saida } } 90 'a nuvem vê o servidor da escola (ping com o token novo)'
    $envAntes = Get-FileHash "$RAIZ\app\.env"
    # o código é de uso único
    try { $r2 = Invoke-WebRequest "$NUVEM/api/espelho/ativar" -Method Post -Body "{`"codigo`": `"$codigo`"}" -ContentType 'application/json' -UseBasicParsing; $st = $r2.StatusCode } catch { $st = [int]$_.Exception.Response.StatusCode }
    Ok ($st -eq 404) 'o mesmo código não serve uma segunda vez'

    # backup agendado roda como SYSTEM
    Start-ScheduledTask -TaskName 'NexosixBackup'
    Esperar { Get-ChildItem "$RAIZ\backups" -Filter 'nexosix-*.dump' -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 0 } } 120 'backup do banco local gerado pela tarefa agendada'

    # o servidor cai sozinho (erro, Windows Update...): volta sozinho
    $svcPid = (Get-CimInstance Win32_Service -Filter "Name='NexosixAgente'").ProcessId
    $filhos = Get-CimInstance Win32_Process -Filter "ParentProcessId=$svcPid"
    $filhos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "  derrubei o python do agente (PID $($filhos.ProcessId -join ','))"
    Start-Sleep -Seconds 3
    Esperar { (ServicosRodando) -and (Http 'http://127.0.0.1/health').StatusCode -eq 200 } 90 'o servidor voltou sozinho depois de cair'
    # e o banco reiniciando (dependências): agente e worker voltam junto
    Restart-Service NexosixBanco -Force
    Start-Service NexosixAgente, NexosixWorker
    Esperar { (ServicosRodando) -and (Http 'http://127.0.0.1/health').StatusCode -eq 200 } 90 'tudo de pé depois de reiniciar o banco'
    GuardarLogs '2-instalacao'

    Passo '3. Atualização (rodar o instalador de novo)'
    Ok ((Setup @("/NUVEM=$NUVEM") 'setup-atualizar.log') -eq 0) 'atualização sai com código 0, sem pedir código'
    Ok ((Get-FileHash "$RAIZ\app\.env").Hash -eq $envAntes.Hash) 'configuração (.env, token) mantida'
    ConferirInstalado 'atualização'
    GuardarLogs '3-atualizacao'

    Passo '4. Desinstalar mantendo os dados e instalar de novo'
    Desinstalar 'desinstalar-4.log'
    Ok (NenhumServico) 'serviços removidos'
    Ok (-not (Get-NetFirewallRule -DisplayName 'Nexosix - servidor da escola' -ErrorAction SilentlyContinue)) 'regra do firewall removida'
    Ok (-not (Get-ScheduledTask -TaskName 'NexosixBackup' -ErrorAction SilentlyContinue)) 'tarefa de backup removida'
    Ok ((Test-Path "$RAIZ\dados\pg\PG_VERSION") -and (Test-Path "$RAIZ\app\.env")) 'banco local e configuração mantidos'
    Ok (-not (Test-Path "$RAIZ\python")) 'programa removido'
    Ok ((Setup @("/NUVEM=$NUVEM") 'setup-reinstalar.log') -eq 0) 'reinstalar sem código continua de onde parou'
    ConferirInstalado 'reinstalação'
    GuardarLogs '4-reinstalacao'

    Passo '5. Desinstalar apagando os dados'
    Desinstalar -ApagarDados 'desinstalar-5.log'
    Ok (NenhumServico) 'serviços removidos'
    Ok (-not (Test-Path $RAIZ)) "nada sobrou em $RAIZ"
    Ok (-not (Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue)) 'porta 80 livre'
    Ok (-not (Get-NetFirewallRule -DisplayName 'Nexosix - servidor da escola' -ErrorAction SilentlyContinue)) 'regra do firewall removida'

    Write-Host "`nTODOS OS TESTES PASSARAM" -ForegroundColor Green
} catch {
    Write-Host "`nERRO: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    GuardarLogs 'falha'
    foreach ($f in (Get-ChildItem "$Saida\logs-falha" -Recurse -File -ErrorAction SilentlyContinue)) {
        Write-Host "`n----- $($f.FullName)" -ForegroundColor Yellow
        Get-Content $f.FullName -Tail 60 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    foreach ($f in (Get-ChildItem $Saida -File -Filter '*.log' -ErrorAction SilentlyContinue)) {
        Write-Host "`n----- $($f.Name)" -ForegroundColor Yellow
        Get-Content $f.FullName -Tail 50 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    Write-Host "`nJanelas abertas: $((JanelasAbertas) -join ' || ')"
    exit 1
} finally {
    if ($nuvemProc) { Stop-Process -Id $nuvemProc.Id -Force -ErrorAction SilentlyContinue }
}
