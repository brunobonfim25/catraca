<#
Instala o servidor local do Nexosix Acesso numa máquina Windows da escola (Modelo 3).

Pré-requisitos (instalar antes, uma vez):
  - Python 3.12  https://www.python.org/downloads/windows/  (marcar "Add to PATH")
  - PostgreSQL 16 https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
    (anote a senha do usuário postgres; crie o banco: createdb -U postgres nexosix)
  - NSSM          https://nssm.cc/download  (nssm.exe em C:\nexosix\nssm.exe)
  - Caddy (HTTPS na LAN, pra webcam) https://caddyserver.com/download (C:\nexosix\caddy.exe)

Uso (PowerShell como Administrador, dentro da pasta do repositório):
  .\deploy\windows\instalar.ps1 -EmpresaId 1 -UnidadeId 1 -Empresa "GEPPP" -Unidade "Recreio"

O script: cria .venv, instala dependências, gera .env (se não existir), roda bootstrap +
configurar-local, registra os serviços NexosixWeb / NexosixWorker / NexosixCaddy no NSSM
(iniciam com o Windows, antes de alguém logar) e agenda o backup diário.
#>
param(
    [int]$EmpresaId = 1,
    [int]$UnidadeId = 1,
    [string]$Empresa = "Empresa",
    [string]$Unidade = "Unidade",
    [string]$Pasta = (Get-Location).Path,
    [string]$Nssm = "C:\nexosix\nssm.exe",
    [string]$Caddy = "C:\nexosix\caddy.exe"
)
$ErrorActionPreference = "Stop"
Set-Location $Pasta

if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\pip.exe install -q -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    $chave = -join ((48..57 + 97..122) | Get-Random -Count 48 | ForEach-Object { [char]$_ })
    (Get-Content .env) -replace "SECRET_KEY=.*", "SECRET_KEY=$chave" -replace "MONITOR_TOKEN=.*", "MONITOR_TOKEN=$(-join ((48..57 + 97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ }))" | Set-Content .env
    Write-Host "Criei .env — EDITE: DATABASE_URL, ADMIN_EMAIL, ADMIN_SENHA, BASE_URL (ex. https://nexosix.local), NUVEM_URL e ESPELHO_TOKEN (gerado na nuvem)." -ForegroundColor Yellow
    notepad .env
    Read-Host "Salvou o .env? Pressione Enter para continuar"
}

$env:FLASK_APP = "wsgi"
.\.venv\Scripts\flask.exe --app wsgi bootstrap
.\.venv\Scripts\flask.exe --app wsgi configurar-local --empresa-id $EmpresaId --unidade-id $UnidadeId --empresa $Empresa --unidade $Unidade

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

Registrar "NexosixWeb" $py "serve.py"
Registrar "NexosixWorker" $py "-m worker.main"
if (Test-Path $Caddy) {
    Copy-Item (Join-Path $Pasta "deploy\windows\Caddyfile") "C:\nexosix\Caddyfile" -Force
    Registrar "NexosixCaddy" $Caddy "run --config C:\nexosix\Caddyfile"
    & $Caddy trust 2>$null
    Write-Host "HTTPS: acesse https://nexosix.local (adicione '<IP deste PC> nexosix.local' no hosts dos PCs da secretaria e rode 'caddy trust' neles, ou aceite o aviso do navegador)."
}

# Backup diário às 23:30 (pg_dump → C:\nexosix\backups, mantém 14 dias)
$acao = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File `"$Pasta\deploy\windows\backup.ps1`""
$gatilho = New-ScheduledTaskTrigger -Daily -At 23:30
Register-ScheduledTask -TaskName "NexosixBackup" -Action $acao -Trigger $gatilho -RunLevel Highest -Force | Out-Null

Write-Host "`nPronto. Painel: http://localhost:8000  (logs em C:\nexosix\logs). Serviços sobem sozinhos ao ligar o PC." -ForegroundColor Green
Write-Host "Próximo: cadastrar a catraca no painel (IP do iDFace primário) → Testar conexão → Configurar Monitor."
