<#
Instala o AGENTE Nexosix numa máquina Windows da escola. O agente só fala com a catraca;
o painel, o cadastro e os relatórios ficam na nuvem.

Pré-requisitos (instalar antes, uma vez):
  - Python 3.12  https://www.python.org/downloads/windows/  (marcar "Add to PATH")
  - PostgreSQL 16 https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
    (anote a senha do usuário postgres; crie o banco: createdb -U postgres nexosix)
  - NSSM          https://nssm.cc/download  (nssm.exe em C:\nexosix\nssm.exe)

Antes, na nuvem: crie a unidade (painel → Unidades) e gere o token:
  flask --app wsgi token-espelho --unidade-id <id>

Uso (PowerShell como Administrador, dentro da pasta do repositório):
  .\deploy\windows\instalar.ps1 -UnidadeId 1

O script: cria .venv, instala dependências, gera .env (se não existir), migra e reserva a
faixa de ids, registra os serviços NexosixAgente / NexosixWorker no NSSM (iniciam com o
Windows, antes de alguém logar) e agenda o backup diário. Na primeira rodada o worker baixa
o cadastro da unidade da nuvem.
#>
param(
    [int]$UnidadeId = 1,
    [string]$Pasta = (Get-Location).Path,
    [string]$Nssm = "C:\nexosix\nssm.exe"
)
$ErrorActionPreference = "Stop"
Set-Location $Pasta

if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

function Aleatorio($n) { -join ((48..57 + 97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    (Get-Content .env) `
        -replace "^MODO=.*", "MODO=local" `
        -replace "^UNIDADE_ID=.*", "UNIDADE_ID=$UnidadeId" `
        -replace "^SECRET_KEY=.*", "SECRET_KEY=$(Aleatorio 48)" `
        -replace "^MONITOR_TOKEN=.*", "MONITOR_TOKEN=$(Aleatorio 32)" | Set-Content .env
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
    Write-Host "Criei .env — EDITE: DATABASE_URL, NUVEM_URL, ESPELHO_TOKEN e BASE_URL (http://${ip} se a catraca usa IP, ou http://catraca-<escola>.nexosix.com.br com a redundância por DNS)." -ForegroundColor Yellow
    notepad .env
    Read-Host "Salvou o .env? Pressione Enter para continuar"
}

.\.venv\Scripts\python.exe -m flask --app wsgi configurar-local --unidade-id $UnidadeId

New-Item -ItemType Directory -Force -Path "C:\nexosix\logs", "C:\nexosix\backups" | Out-Null
$py = Join-Path $Pasta ".venv\Scripts\python.exe"

function Registrar($nome, $exe, $args) {
    & $Nssm stop $nome 2>$null | Out-Null
    & $Nssm remove $nome confirm 2>$null | Out-Null
    & $Nssm install $nome $exe $args
    & $Nssm set $nome AppDirectory $Pasta
    & $Nssm set $nome AppStdout "C:\nexosix\logs\$nome.log"
    & $Nssm set $nome AppStderr "C:\nexosix\logs\$nome.log"
    & $Nssm set $nome AppRotateFiles 1
    & $Nssm set $nome AppRotateBytes 10485760
    & $Nssm set $nome Start SERVICE_AUTO_START
    & $Nssm set $nome AppRestartDelay 5000
    & $Nssm start $nome
}

Registrar "NexosixAgente" $py "serve.py"
Registrar "NexosixWorker" $py "-m worker.main"

# Firewall: a catraca precisa alcançar a porta do agente (PORT no .env; padrão 80)
$porta = ((Get-Content .env | Where-Object { $_ -match "^PORT=" }) -replace "^PORT=", "")
if (-not $porta) { $porta = "80" }
netsh advfirewall firewall delete rule name="Nexosix Agente" | Out-Null
netsh advfirewall firewall add rule name="Nexosix Agente" dir=in action=allow protocol=TCP localport=$porta | Out-Null

# Backup diário às 23:30 (pg_dump → C:\nexosix\backups, mantém 14 dias)
$acao = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File `"$Pasta\deploy\windows\backup.ps1`""
$gatilho = New-ScheduledTaskTrigger -Daily -At 23:30
Register-ScheduledTask -TaskName "NexosixBackup" -Action $acao -Trigger $gatilho -RunLevel Highest -Force | Out-Null

Write-Host "`nPronto. Status do agente: http://localhost:$porta/agente  (logs em C:\nexosix\logs)." -ForegroundColor Green
Write-Host "Na nuvem: Catracas → cadastrar com o IP do iDFace → Testar conexão → Configurar Monitor. O agente executa em segundos."
