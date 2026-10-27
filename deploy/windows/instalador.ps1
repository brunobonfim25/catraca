<#
Instalador do AGENTE Nexosix para Windows — um comando só, sem pré-requisitos.
Gerado pelo painel da nuvem com os dados da unidade já dentro (placeholders __X__).

O que faz (idempotente: rodar de novo = atualizar o agente):
  1. Python 3.12 (instalação silenciosa, se não houver)
  2. PostgreSQL 16 (instalação silenciosa, se não houver) + banco "nexosix"
  3. Baixa o código do agente (da nuvem, com o token) para C:\nexosix\app e instala as dependências
  4. Escreve C:\nexosix\app\.env (UNIDADE_ID, NUVEM_URL, ESPELHO_TOKEN, BASE_URL…)
  5. Migra o banco e reserva a faixa de ids da unidade
  6. Registra os serviços NexosixAgente e NexosixWorker (NSSM; sem NSSM, tarefas agendadas)
  7. Abre a porta no firewall e agenda o backup diário
#>
param([switch]$SoAtualizar)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$NUVEM_URL   = "__NUVEM_URL__"
$TOKEN       = "__TOKEN__"
$UNIDADE_ID  = "__UNIDADE_ID__"
$UNIDADE     = "__UNIDADE_NOME__"
$PY_URL      = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$PG_URL      = "https://get.enterprisedb.com/postgresql/postgresql-16.9-1-windows-x64.exe"
$NSSM_URL    = "https://nssm.cc/release/nssm-2.24.zip"
$RAIZ        = "C:\nexosix"
$APP         = "$RAIZ\app"
$PORTA       = "80"

function Passo($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Aleatorio($n) { -join ((48..57 + 65..90 + 97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Abra o PowerShell como ADMINISTRADOR e rode de novo." -ForegroundColor Red; exit 1
}
New-Item -ItemType Directory -Force -Path $RAIZ, "$RAIZ\logs", "$RAIZ\backups", "$RAIZ\tmp" | Out-Null
# .env (tokens, senha do banco), pg-senha.txt e backups ficam aqui: só SYSTEM e Administradores leem.
# Antes qualquer usuário do PC da recepção abria o .env e virava o "agente" da escola.
icacls $RAIZ /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
Write-Host "Agente Nexosix — unidade $UNIDADE_ID ($UNIDADE) — nuvem $NUVEM_URL" -ForegroundColor Green

# ---------------------------------------------------------------- 1. Python
Passo "Python 3.12"
$py = Get-ChildItem "C:\Program Files\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $py) {
    Invoke-WebRequest $PY_URL -OutFile "$RAIZ\tmp\python.exe"
    Start-Process "$RAIZ\tmp\python.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_launcher=0" -Wait
    $py = Get-Item "C:\Program Files\Python312\python.exe"
}
$py = $py.FullName
Write-Host "   $py"

# ---------------------------------------------------------------- 2. PostgreSQL
Passo "PostgreSQL 16"
$pgSenhaArq = "$RAIZ\pg-senha.txt"
if (-not (Get-Service postgresql-x64-16 -ErrorAction SilentlyContinue)) {
    $pgSenha = Aleatorio 20
    Set-Content $pgSenhaArq $pgSenha
    Invoke-WebRequest $PG_URL -OutFile "$RAIZ\tmp\postgresql.exe"
    Start-Process "$RAIZ\tmp\postgresql.exe" -ArgumentList "--mode unattended --unattendedmodeui none --superpassword `"$pgSenha`" --servicename postgresql-x64-16 --serverport 5432 --disable-components pgAdmin,stackbuilder --prefix `"C:\Program Files\PostgreSQL\16`" --datadir `"C:\Program Files\PostgreSQL\16\data`"" -Wait
} elseif (-not (Test-Path $pgSenhaArq)) {
    $pgSenha = Read-Host "PostgreSQL já instalado. Senha do usuário postgres"
    Set-Content $pgSenhaArq $pgSenha
}
$pgSenha = (Get-Content $pgSenhaArq -Raw).Trim()
$psql = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
$env:PGPASSWORD = $pgSenha
Start-Service postgresql-x64-16 -ErrorAction SilentlyContinue
$existe = & $psql -U postgres -h 127.0.0.1 -tAc "SELECT 1 FROM pg_database WHERE datname='nexosix'" 2>$null
if ($existe -ne "1") { & $psql -U postgres -h 127.0.0.1 -c "CREATE DATABASE nexosix" | Out-Null }
Write-Host "   banco nexosix ok"

# ---------------------------------------------------------------- 3. Código do agente
Passo "Código do agente"
# Código vem da nuvem (token no cabeçalho), não do GitHub: o repositório pode ser privado
Invoke-WebRequest "$NUVEM_URL/api/espelho/pacote.zip" -Headers @{ "X-Espelho-Token" = $TOKEN } -UseBasicParsing -OutFile "$RAIZ\tmp\app.zip"
Remove-Item "$RAIZ\tmp\app" -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive "$RAIZ\tmp\app.zip" "$RAIZ\tmp\app"
$origem = Get-ChildItem "$RAIZ\tmp\app" | Select-Object -First 1
if (Test-Path "$APP\.env") { Copy-Item "$APP\.env" "$RAIZ\tmp\env.bak" }
# Atualização: antes de parar, sobe pra nuvem o que ainda não subiu (eventos da manhã etc.).
# Revisão (E10): parar no meio deixava eventos só no PC; se a reinstalação desse errado, sumiam.
if (Test-Path "$APP\.venv\Scripts\python.exe") {
    Push-Location $APP
    & "$APP\.venv\Scripts\python.exe" -m flask --app wsgi espelho-enviar 2>&1 | Out-Host
    Pop-Location
}
foreach ($nome in "NexosixAgente", "NexosixWorker") { Stop-Service $nome -ErrorAction SilentlyContinue; schtasks /end /tn $nome 2>$null | Out-Null }
New-Item -ItemType Directory -Force -Path $APP | Out-Null
Get-ChildItem $APP -Exclude ".env", ".venv" | Remove-Item -Recurse -Force
Copy-Item "$($origem.FullName)\*" $APP -Recurse -Force
if (Test-Path "$RAIZ\tmp\env.bak") { Copy-Item "$RAIZ\tmp\env.bak" "$APP\.env" -Force }
Set-Location $APP
if (-not (Test-Path "$APP\.venv")) { & $py -m venv "$APP\.venv" }
& "$APP\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$APP\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
Write-Host "   dependências ok"

# ---------------------------------------------------------------- 4. .env
Passo "Configuração (.env)"
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" -and $_.PrefixOrigin -ne "WellKnown" } | Sort-Object InterfaceMetric | Select-Object -First 1).IPAddress
if (-not (Test-Path "$APP\.env")) {
    @"
MODO=local
UNIDADE_ID=$UNIDADE_ID
NUVEM_URL=$NUVEM_URL
ESPELHO_TOKEN=$TOKEN
DATABASE_URL=postgresql://postgres:$pgSenha@127.0.0.1:5432/nexosix
SECRET_KEY=$(Aleatorio 48)
MONITOR_TOKEN=$(Aleatorio 32)
BASE_URL=http://$ip
PORT=$PORTA
TZ_PADRAO=America/Sao_Paulo
WORKER_INTERVALO_SEG=3
"@ | Set-Content "$APP\.env" -Encoding ASCII
    Write-Host "   .env criado (IP $ip)"
} else {
    # token/nuvem podem ter mudado no painel; o resto (segredos locais) fica
    $env_ = Get-Content "$APP\.env"
    $env_ = $env_ -replace "^ESPELHO_TOKEN=.*", "ESPELHO_TOKEN=$TOKEN" -replace "^NUVEM_URL=.*", "NUVEM_URL=$NUVEM_URL" -replace "^UNIDADE_ID=.*", "UNIDADE_ID=$UNIDADE_ID"
    # IP fixado na instalação ficava velho quando o roteador trocava o IP; agora o agente descobre a cada ping
    $env_ = $env_ | Where-Object { $_ -notmatch "^AGENTE_IP_LAN=" }
    Set-Content "$APP\.env" $env_ -Encoding ASCII
    Write-Host "   .env mantido (token/nuvem atualizados)"
}

# ---------------------------------------------------------------- 5. Banco
Passo "Migrações e faixa de ids"
& "$APP\.venv\Scripts\python.exe" -m flask --app wsgi configurar-local --unidade-id $UNIDADE_ID

# ---------------------------------------------------------------- 6. Serviços
Passo "Serviços"
$nssm = "$RAIZ\nssm.exe"
if (-not (Test-Path $nssm)) {
    try {
        Invoke-WebRequest $NSSM_URL -OutFile "$RAIZ\tmp\nssm.zip" -TimeoutSec 60
        Expand-Archive "$RAIZ\tmp\nssm.zip" "$RAIZ\tmp\nssm" -Force
        Copy-Item (Get-ChildItem "$RAIZ\tmp\nssm" -Recurse -Filter nssm.exe | Where-Object { $_.FullName -like "*win64*" } | Select-Object -First 1).FullName $nssm
    } catch { Write-Host "   NSSM indisponível; usando tarefas agendadas" -ForegroundColor Yellow }
}
$pyv = "$APP\.venv\Scripts\python.exe"
if (Test-Path $nssm) {
    foreach ($svc in @(@("NexosixAgente", "serve.py"), @("NexosixWorker", "-m worker.main"))) {
        $nome, $args = $svc
        & $nssm stop $nome 2>$null | Out-Null; & $nssm remove $nome confirm 2>$null | Out-Null
        & $nssm install $nome $pyv $args | Out-Null
        & $nssm set $nome AppDirectory $APP | Out-Null
        & $nssm set $nome AppStdout "$RAIZ\logs\$nome.log" | Out-Null
        & $nssm set $nome AppStderr "$RAIZ\logs\$nome.log" | Out-Null
        & $nssm set $nome AppRotateFiles 1 | Out-Null
        & $nssm set $nome AppRotateBytes 10485760 | Out-Null
        & $nssm set $nome Start SERVICE_AUTO_START | Out-Null
        & $nssm set $nome AppRestartDelay 5000 | Out-Null
        & $nssm set $nome DependOnService postgresql-x64-16 | Out-Null
        & $nssm start $nome | Out-Null
    }
    Write-Host "   serviços NexosixAgente e NexosixWorker registrados (NSSM)"
} else {
    # Sem NSSM: tarefas que sobem com o Windows (sem login) e um vigia a cada 5 min que religa se cair
    foreach ($svc in @(@("NexosixAgente", "serve.py"), @("NexosixWorker", "-m worker.main"))) {
        $nome, $args = $svc
        $acao = New-ScheduledTaskAction -Execute $pyv -Argument $args -WorkingDirectory $APP
        $gat = New-ScheduledTaskTrigger -AtStartup
        $cfg = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $nome -Action $acao -Trigger $gat -Settings $cfg -User "SYSTEM" -RunLevel Highest -Force | Out-Null
        Start-ScheduledTask -TaskName $nome
    }
    $vigia = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"foreach (`$t in 'NexosixAgente','NexosixWorker') { if ((Get-ScheduledTask `$t).State -ne 'Running') { Start-ScheduledTask `$t } }`""
    Register-ScheduledTask -TaskName "NexosixVigia" -Action $vigia -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)) -User "SYSTEM" -RunLevel Highest -Force | Out-Null
    Write-Host "   tarefas NexosixAgente, NexosixWorker e NexosixVigia registradas"
}

# ---------------------------------------------------------------- 7. Firewall e backup
Passo "Firewall e backup"
netsh advfirewall firewall delete rule name="Nexosix Agente" | Out-Null
netsh advfirewall firewall add rule name="Nexosix Agente" dir=in action=allow protocol=TCP localport=$PORTA | Out-Null
$bk = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$APP\deploy\windows\backup.ps1`""
Register-ScheduledTask -TaskName "NexosixBackup" -Action $bk -Trigger (New-ScheduledTaskTrigger -Daily -At 23:30) -User "SYSTEM" -RunLevel Highest -Force | Out-Null
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null

Remove-Item "$RAIZ\tmp" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "`nPRONTO. Agente da unidade $UNIDADE instalado." -ForegroundColor Green
Write-Host "  Status local : http://$ip`:$PORTA/agente"
Write-Host "  Painel       : $NUVEM_URL  (em ~1 min a unidade deve aparecer como 'agente visto')"
Write-Host "  Logs         : $RAIZ\logs"
Write-Host "  A catraca é configurada pelo painel (Catracas > Configurar Monitor) com o NOME da escola, não com o IP."
Write-Host "  IMPORTANTE: no roteador da escola, crie uma entrada de DNS local com esse nome apontando para $ip." -ForegroundColor Yellow
Write-Host "  Sem ela, se a internet cair a catraca não acha este PC (o nome só existe na internet)." -ForegroundColor Yellow
Start-Process "http://localhost:$PORTA/agente"
