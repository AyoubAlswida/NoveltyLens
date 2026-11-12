[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot 'runtime.env'),
    [ValidateRange(1024,65535)][int]$Port = 4200,
    [string]$Name = 'novelty-lens',
    [string]$Volume = 'novelty-lens-data',
    [string]$CaFile = ''
)
$ErrorActionPreference = 'Stop'
function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($Arguments[0])" }
}
$osType = Invoke-Docker -Arguments @('info','--format','{{.OSType}}')
if ($osType -ne 'linux') { throw 'Switch Docker to Linux containers before starting.' }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw 'Copy runtime.env.example to runtime.env and enter your endpoint settings and API keys.'
}
if (Select-String -LiteralPath $EnvFile -Pattern '^\s*[A-Z_]+=REPLACE_' -Quiet) {
    throw 'Fill all REPLACE_ values in runtime.env before starting.'
}
$manifest = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'image.json') | ConvertFrom-Json
$tar = Join-Path $PSScriptRoot 'novelty-lens-image.tar'
$expected = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'image.sha256')).Trim()
if ((Get-FileHash -LiteralPath $tar -Algorithm SHA256).Hash -ne $expected) {
    throw 'Image checksum mismatch. Transfer/extract the ZIP again.'
}
Invoke-Docker -Arguments @('load','--input',$tar)
$actual = & docker image inspect $manifest.tag --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $actual.Trim() -ne $manifest.id) {
    throw 'Loaded image identity does not match image.json.'
}
$existing = & docker ps -a --filter "name=^/$Name$" --format '{{.Names}}'
if ($existing) {
    throw "Container $Name already exists. Use docker start $Name or docker restart $Name. To change settings: docker stop -t 90 $Name then docker rm $Name, then rerun this script; the volume is kept."
}
$runArgs = @('run','--detach','--pull=never','--name',$Name,
             '--stop-timeout','90','--env-file',(Resolve-Path -LiteralPath $EnvFile).Path,
             '--publish',"127.0.0.1:${Port}:8080",'--mount',"type=volume,source=$Volume,target=/data")
if ($CaFile) {
    $ca = (Resolve-Path -LiteralPath $CaFile).Path
    $runArgs += @('--mount',"type=bind,source=$ca,target=/certs/company-ca.pem,readonly",
                  '--env','REQUESTS_CA_BUNDLE=/certs/company-ca.pem',
                  '--env','SSL_CERT_FILE=/certs/company-ca.pem')
}
$runArgs += $manifest.tag
Invoke-Docker -Arguments $runArgs
$deadline = (Get-Date).AddSeconds(180)
do {
    $state = & docker inspect $Name --format '{{.State.Status}} {{.State.Health.Status}}'
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect container.' }
    if ($state -eq 'running healthy') {
        Write-Host "Ready: http://localhost:$Port"
        exit 0
    }
    if ($state -notlike 'running *') {
        & docker logs --tail 40 $Name
        throw "Container did not start: $state"
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
& docker logs --tail 40 $Name
throw 'Container did not become healthy in 180 seconds. Inspect docker logs; no successful start was reported.'
