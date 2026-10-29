# Backup diário do banco local (tarefa NexosixBackup, 23:30, como SYSTEM). Além disso tudo já
# está na nuvem; este é para o caso de o PC ficar sem internet por dias.
# Guarda 14 dias em C:\nexosix\backups.
. (Join-Path $PSScriptRoot 'comum.ps1')
$LOG = Join-Path $LOGS 'backup.log'
$script:EtapaLog = 'backup'
try {
    New-Item -ItemType Directory -Force -Path $BACKUPS | Out-Null
    $arquivo = Join-Path $BACKUPS ('nexosix-{0:yyyyMMdd-HHmm}.dump' -f (Get-Date))
    Rodar (Join-Path $PGBIN 'pg_dump.exe') @('-h', '127.0.0.1', '-p', "$PORTA_PG", '-U', 'postgres', '-d', 'nexosix', '--format=custom', "--file=$arquivo") -Ambiente @{ PGPASSWORD = (SenhaDoBanco) } | Out-Null
    # Revisão (E17): só apaga os antigos se o de hoje existe e não está vazio
    if (-not (Test-Path $arquivo) -or (Get-Item $arquivo).Length -eq 0) { throw 'pg_dump não gerou o arquivo' }
    Get-ChildItem $BACKUPS -Filter 'nexosix-*.dump' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force
    Log "backup ok: $arquivo ($([int]((Get-Item $arquivo).Length / 1KB)) KB)"
    exit 0
} catch {
    Log "ERRO: $($_.Exception.Message) (backups antigos mantidos)"
    exit 1
}
# Restaurar: C:\nexosix\pgsql\bin\pg_restore -h 127.0.0.1 -p 5439 -U postgres -d nexosix --clean --if-exists <arquivo>
