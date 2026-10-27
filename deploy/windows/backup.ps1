# Backup diário do banco local (além do espelho na nuvem). Lê DATABASE_URL do .env.
# Guarda 14 dias em C:\nexosix\backups. Copie a pasta pra um pendrive/NAS de vez em quando.
$ErrorActionPreference = "Stop"
$pasta = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env_ = Get-Content (Join-Path $pasta ".env") | Where-Object { $_ -match "^DATABASE_URL=" }
$url = $env_ -replace "^DATABASE_URL=", ""
$destino = "C:\nexosix\backups"
New-Item -ItemType Directory -Force -Path $destino | Out-Null
$arquivo = Join-Path $destino ("nexosix-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".dump")
$pgdump = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" | Select-Object -First 1
& $pgdump.FullName --dbname=$url --format=custom --file=$arquivo
# Revisão (E17): o pg_dump falhando (senha trocada, disco cheio) não para o script — os backups
# antigos eram apagados mesmo sem um novo. Só limpa se o de hoje existe e não está vazio.
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $arquivo) -or (Get-Item $arquivo).Length -eq 0) {
    Write-Error "pg_dump falhou (código $LASTEXITCODE): backups antigos mantidos"
    exit 1
}
Get-ChildItem $destino -Filter "nexosix-*.dump" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item
Write-Host "backup ok: $arquivo"
# Restaurar: pg_restore --dbname=<DATABASE_URL> --clean --if-exists <arquivo>
