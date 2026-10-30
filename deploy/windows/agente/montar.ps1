<#
Monta a pasta que vira o instalador (roda no Windows do GitHub Actions, ver agente-windows.yml).
Baixa versões fixas de Python embutível, PostgreSQL (binários, sem instalador), WinSW e o
runtime do Visual C++, instala as dependências do programa e confere que tudo importa.

  .\deploy\windows\agente\montar.ps1 [-Saida build\agente]

Para trocar de versão: mude as constantes abaixo e o SHA-256 (o build falha se o arquivo baixado
não for o esperado — um download adulterado não pode virar instalador assinado de escola).
#>
param([string]$Saida = (Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'build\agente'))
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$REPO = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$PY_VERSAO = '3.13.14'
$PG_VERSAO = '16.15-1'
# Auditoria de 30/10: os hashes eram variáveis de ambiente que o workflow nunca definia — a
# conferência nunca rodava. Fixos aqui (medidos na build 1.0.1); mudou o arquivo, a build falha.
$DOWNLOADS = @(
    @{ Nome = 'python.zip'; Url = "https://www.python.org/ftp/python/$PY_VERSAO/python-$PY_VERSAO-embed-amd64.zip"; Sha = '90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907' },
    @{ Nome = 'pgsql.zip'; Url = "https://get.enterprisedb.com/postgresql/postgresql-$PG_VERSAO-windows-x64-binaries.zip"; Sha = '25e6fcdfb8caec38691bf461125e7564508760666f7b8e5dc6a5f0818f58f81e' },
    @{ Nome = 'WinSW.exe'; Url = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe'; Sha = 'b5066b7bbdfba1293e5d15cda3caaea88fbeab35bd5b38c41c913d492aadfc4f' },
    @{ Nome = 'vc_redist.x64.exe'; Url = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'; Sha = 'cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b' }
)

function Passo($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Nativo([string]$Exe, [string[]]$Argumentos) {
    # PowerShell 5.1 + Stop: aviso no stderr (o pip escreve vários) viraria exceção; vale o código de saída
    $antes = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $Exe @Argumentos 2>&1 | ForEach-Object { Write-Host "  $_" }; $c = $LASTEXITCODE } finally { $ErrorActionPreference = $antes }
    if ($c -ne 0) { throw "$Exe terminou com código $c" }
}

$cache = Join-Path $REPO 'build\downloads'
New-Item -ItemType Directory -Force -Path $cache | Out-Null
if (Test-Path $Saida) { Remove-Item $Saida -Recurse -Force }
foreach ($d in 'app', 'python', 'pgsql', 'servicos', 'instalador') { New-Item -ItemType Directory -Force -Path (Join-Path $Saida $d) | Out-Null }

Passo 'Downloads'
$hashes = @()
foreach ($d in $DOWNLOADS) {
    $arq = Join-Path $cache $d.Nome
    if (-not (Test-Path $arq)) {
        for ($t = 1; $t -le 4; $t++) {
            try { Invoke-WebRequest $d.Url -OutFile $arq -UseBasicParsing -TimeoutSec 600; break }
            catch { if ($t -eq 4) { throw }; Write-Host "  falhou ($($_.Exception.Message)), tentando de novo"; Start-Sleep -Seconds (2 * $t) }
        }
    }
    $sha = (Get-FileHash $arq -Algorithm SHA256).Hash.ToLower()
    $hashes += "$sha  $($d.Nome)  $($d.Url)"
    Write-Host "  $($d.Nome): $sha"
    if ($d.Sha -and $d.Sha.ToLower() -ne $sha) { throw "SHA-256 de $($d.Nome) não confere: esperado $($d.Sha), veio $sha" }
}

Passo "Python $PY_VERSAO (embutível)"
$py = Join-Path $Saida 'python'
Expand-Archive (Join-Path $cache 'python.zip') $py -Force
# ._pth: o Python embutível só enxerga o que está listado aqui (nem PYTHONPATH, nem a pasta atual).
# ..\app = C:\nexosix\app; "import site" liga o Lib\site-packages.
$pth = Get-ChildItem $py -Filter 'python3*._pth' | Select-Object -First 1
$zip = (Get-ChildItem $py -Filter 'python3*.zip' | Select-Object -First 1).Name
Set-Content -Path $pth.FullName -Encoding ASCII -Value @($zip, '.', 'Lib\site-packages', '..\app', 'import site')
$site = Join-Path $py 'Lib\site-packages'
New-Item -ItemType Directory -Force -Path $site | Out-Null

Passo 'Dependências do programa'
# As mesmas versões da nuvem (requirements.txt), sem o que só serve lá ou nos testes
$req = Join-Path $env:TEMP 'requirements-agente.txt'
Get-Content (Join-Path $REPO 'requirements.txt') | Where-Object { $_ -notmatch '^(pytest|gunicorn)\b' } | Set-Content $req -Encoding ASCII
Nativo 'python' @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location', '--only-binary=:all:', '--target', $site, '-r', $req)
Remove-Item (Join-Path $site 'bin') -Recurse -Force -ErrorAction SilentlyContinue

Passo 'Programa'
$app = Join-Path $Saida 'app'
foreach ($item in 'app', 'worker', 'integrations', 'migrations') {
    Copy-Item (Join-Path $REPO $item) (Join-Path $app $item) -Recurse -Force
}
foreach ($item in 'serve.py', 'wsgi.py', 'requirements.txt') { Copy-Item (Join-Path $REPO $item) $app -Force }
Get-ChildItem $app -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Get-ChildItem $app -Recurse -File -Include '*.pyc', '.env' | Remove-Item -Force

Passo "PostgreSQL $PG_VERSAO (binários)"
$tmp = Join-Path $env:TEMP 'pgsql-extraido'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
# tar.exe (vem no Windows) extrai só o que interessa; o Expand-Archive do PowerShell 5.1 leva
# dezenas de minutos no zip inteiro (milhares de arquivos do pgAdmin)
Nativo (Join-Path $env:SystemRoot 'System32\tar.exe') @('-xf', (Join-Path $cache 'pgsql.zip'), '-C', $tmp, 'pgsql/bin', 'pgsql/lib', 'pgsql/share', 'pgsql/server_license.txt')
$origem = Join-Path $tmp 'pgsql'
# Só o servidor e as ferramentas de linha de comando: sem pgAdmin (centenas de MB), StackBuilder,
# documentação, cabeçalhos de C e símbolos de depuração
foreach ($d in 'bin', 'lib', 'share') {
    Copy-Item (Join-Path $origem $d) (Join-Path $Saida "pgsql\$d") -Recurse -Force
}
Get-ChildItem (Join-Path $Saida 'pgsql') -Recurse -File -Include '*.pdb', '*.lib', '*.a' | Remove-Item -Force
# StackBuilder (com a interface wx*) e programas de teste do próprio PostgreSQL também não servem aqui
Get-ChildItem (Join-Path $Saida 'pgsql\bin\*') -File -Include 'stackbuilder.exe', 'wx*.dll', '*regress*.exe', 'isolationtester.exe', 'libpq_*.exe', 'test*.exe', 'testplug.dll' | Remove-Item -Force
Remove-Item (Join-Path $Saida 'pgsql\share\doc') -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $origem 'server_license.txt') (Join-Path $Saida 'pgsql\LICENSE.txt')

Passo 'Serviços e scripts do instalador'
Copy-Item (Join-Path $cache 'WinSW.exe') (Join-Path $Saida 'servicos\WinSW.exe')
Copy-Item (Join-Path $cache 'vc_redist.x64.exe') (Join-Path $Saida 'instalador\vc_redist.x64.exe')
foreach ($s in 'comum.ps1', 'configurar.ps1', 'remover.ps1', 'backup.ps1') {
    $f = Join-Path $PSScriptRoot $s
    $bytes = [IO.File]::ReadAllBytes($f)
    if ($bytes.Length -lt 3 -or $bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
        throw "$s precisa estar em UTF-8 com BOM (o PowerShell 5.1 lê sem BOM como ANSI e estraga os acentos)"
    }
    Copy-Item $f (Join-Path $Saida "instalador\$s")
}
$versao = if ($env:AGENTE_VERSAO) { $env:AGENTE_VERSAO } else { '0.0.0-dev' }
$commit = if ($env:GITHUB_SHA) { $env:GITHUB_SHA.Substring(0, 7) } else { 'local' }
Set-Content (Join-Path $Saida 'VERSAO.txt') -Encoding ASCII -Value @("versao=$versao", "commit=$commit", "python=$PY_VERSAO", "postgresql=$PG_VERSAO", "montado=$((Get-Date).ToUniversalTime().ToString('s'))Z")
Set-Content (Join-Path $REPO 'build\SHA256-dependencias.txt') -Encoding ASCII -Value $hashes

Passo 'Conferindo a pasta montada'
$pyexe = Join-Path $py 'python.exe'
Nativo $pyexe @('-c', "import sys, flask, psycopg2, waitress, tzdata, cryptography, requests, openpyxl, qrcode; from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo'); import app, worker.main; print('python', sys.version.split()[0], 'importa o programa')")
Nativo (Join-Path $Saida 'pgsql\bin\postgres.exe') @('--version')
$mb = [int]((Get-ChildItem $Saida -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "`nPasta montada: $Saida ($mb MB antes de comprimir)" -ForegroundColor Green
