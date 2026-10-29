# Caminhos e funções usados por configurar.ps1, remover.ps1 e backup.ps1.
# Roda no Windows PowerShell 5.1 (o que vem no Windows 10/11): nada de recurso só do PowerShell 7.
# Arquivo salvo em UTF-8 COM BOM: sem o BOM o 5.1 lê como ANSI e os acentos viram lixo.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$RAIZ    = Split-Path -Parent $PSScriptRoot          # C:\nexosix
$APP     = Join-Path $RAIZ 'app'
$ENV_ARQ = Join-Path $APP '.env'
$PY      = Join-Path $RAIZ 'python\python.exe'
$PGBIN   = Join-Path $RAIZ 'pgsql\bin'
$DADOS   = Join-Path $RAIZ 'dados\pg'
$LOGS    = Join-Path $RAIZ 'logs'
$BACKUPS = Join-Path $RAIZ 'backups'
$SERVICOS = Join-Path $RAIZ 'servicos'
$PORTA    = 80
# Porta própria do banco: o PC da escola pode já ter um PostgreSQL na 5432 (outro sistema)
$PORTA_PG = 5439
$SVC_BANCO  = 'NexosixBanco'
$SVC_AGENTE = 'NexosixAgente'
$SVC_WORKER = 'NexosixWorker'
$REGRA_FIREWALL = 'Nexosix - servidor da escola'
$TAREFA_BACKUP  = 'NexosixBackup'

New-Item -ItemType Directory -Force -Path $LOGS | Out-Null
$LOG = Join-Path $LOGS 'instalacao.log'
$script:EtapaLog = 'geral'

function Log([string]$m) {
    $linha = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $script:EtapaLog, $m
    Add-Content -Path $LOG -Value $linha -Encoding UTF8
    Write-Host $linha
}

function Rodar([string]$Exe, [string[]]$Argumentos = @(), [switch]$PodeFalhar, [string]$Dir, [hashtable]$Ambiente) {
    <# Roda um programa e grava a saída no log. No PowerShell 5.1, com ErrorActionPreference=Stop,
       qualquer linha que um programa escreve no stderr vira exceção — o instalador antigo
       morria no "schtasks /end" de uma tarefa que ainda não existia. Aqui o erro é o código de saída. #>
    $antes = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $guardado = @{}
    $codificacao = $null
    # Programas daqui (python, psql) escrevem em UTF-8; sem isto o PowerShell lê na página de
    # código do console e "Itaguaí" vira "Itagua├¡" no log e na tela final
    try { $codificacao = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    try {
        if ($Ambiente) { foreach ($k in $Ambiente.Keys) { $guardado[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $Ambiente[$k]) } }
        if ($Dir) { Push-Location $Dir }
        $saida = & $Exe @Argumentos 2>&1 | ForEach-Object { "$_" }
        $codigo = $LASTEXITCODE
    } finally {
        if ($Dir) { Pop-Location }
        foreach ($k in $guardado.Keys) { [Environment]::SetEnvironmentVariable($k, $guardado[$k]) }
        if ($codificacao) { try { [Console]::OutputEncoding = $codificacao } catch { } }
        $ErrorActionPreference = $antes
    }
    foreach ($l in $saida) { Add-Content -Path $LOG -Value "    $l" -Encoding UTF8 }
    if ($codigo -ne 0 -and -not $PodeFalhar) {
        $fim = ($saida | Select-Object -Last 3) -join ' | '
        throw "$([IO.Path]::GetFileName($Exe)) terminou com código ${codigo}: $fim"
    }
    return $codigo
}

function Segredo([int]$n) {
    $letras = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $b = New-Object byte[] $n
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($b)
    -join ($b | ForEach-Object { $letras[$_ % $letras.Length] })
}

function LerEnv {
    $h = @{}
    if (Test-Path $ENV_ARQ) {
        foreach ($l in Get-Content $ENV_ARQ) {
            if ($l -match '^\s*([A-Z_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $h
}

function SenhaDoBanco {
    $url = (LerEnv)['DATABASE_URL']
    if ($url -match '^postgresql://postgres:([^@]+)@') { return $Matches[1] }
    throw "DATABASE_URL do $ENV_ARQ não está no formato esperado"
}

function Psql([string]$Sql, [string]$Banco = 'postgres') {
    $antes = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $codificacao = $null
    try { $codificacao = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    try {
        $env:PGPASSWORD = SenhaDoBanco
        $env:PGCLIENTENCODING = 'UTF8'
        $saida = & (Join-Path $PGBIN 'psql.exe') -w -h 127.0.0.1 -p $PORTA_PG -U postgres -d $Banco -tAc $Sql 2>$null
    } finally {
        Remove-Item Env:\PGPASSWORD, Env:\PGCLIENTENCODING -ErrorAction SilentlyContinue
        if ($codificacao) { try { [Console]::OutputEncoding = $codificacao } catch { } }
        $ErrorActionPreference = $antes
    }
    return "$saida".Trim()
}

function ServicoExiste([string]$Nome) { return [bool](Get-Service -Name $Nome -ErrorAction SilentlyContinue) }

function EsperarServico([string]$Nome, [string]$Estado, [int]$Segundos = 60) {
    $fim = (Get-Date).AddSeconds($Segundos)
    while ((Get-Date) -lt $fim) {
        $s = Get-Service -Name $Nome -ErrorAction SilentlyContinue
        if ($Estado -eq 'Ausente' -and -not $s) { return $true }
        if ($s -and "$($s.Status)" -eq $Estado) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function PararServico([string]$Nome) {
    if (-not (ServicoExiste $Nome)) { return }
    $s = Get-Service -Name $Nome
    if ("$($s.Status)" -ne 'Stopped') {
        Log "parando $Nome"
        try { Stop-Service -Name $Nome -Force -ErrorAction Stop } catch { Log "  Stop-Service: $($_.Exception.Message)" }
        if (-not (EsperarServico $Nome 'Stopped' 60)) { throw "o serviço $Nome não parou em 60 s" }
    }
}

function GravarErro([string]$m) {
    # Lido pelo instalador (Inno Setup) para mostrar na tela: ANSI do sistema, que é o que ele lê
    Set-Content -Path (Join-Path $LOGS 'instalacao-erro.txt') -Value $m -Encoding Default
}

function Etapa([string]$Nome, [scriptblock]$Corpo) {
    $script:EtapaLog = $Nome
    Remove-Item (Join-Path $LOGS 'instalacao-erro.txt') -ErrorAction SilentlyContinue
    try {
        & $Corpo
        Log 'ok'
        exit 0
    } catch {
        $m = $_.Exception.Message
        Log "ERRO: $m"
        Log ($_.ScriptStackTrace -replace "`r?`n", ' <- ')
        GravarErro $m
        exit 1
    }
}
