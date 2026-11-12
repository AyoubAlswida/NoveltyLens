[CmdletBinding()]
param(
    [string]$Tag = 'novelty-lens:aramco',
    [string]$DockerContext = '',
    [string]$BuildCaFile = '',
    [string]$DependencyImage = '',
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$output = Join-Path $repo '.artifacts\aramco-docker'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$dockerOptions = @()
if ($DockerContext) { $dockerOptions = @('--context',$DockerContext) }
function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @dockerOptions @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($Arguments[0])" }
}
$osType = Invoke-Docker -Arguments @('info','--format','{{.OSType}}')
if ($osType -ne 'linux') { throw 'Switch Docker to Linux containers before building.' }
if (-not $SkipBuild) {
    $buildArgs = @('build','--platform','linux/amd64','--file',
        (Join-Path $PSScriptRoot 'Dockerfile'),'--tag',$Tag)
    if ($BuildCaFile) {
        $buildArgs += @('--secret',"id=build_ca,src=$((Resolve-Path -LiteralPath $BuildCaFile).Path)")
    }
    if ($DependencyImage) { $buildArgs += @('--build-arg',"DEPENDENCY_IMAGE=$DependencyImage") }
    $buildArgs += $repo
    Invoke-Docker -Arguments $buildArgs
}
$raw = & docker @dockerOptions image inspect $Tag
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect built image.' }
$metadata = ($raw | ConvertFrom-Json)[0]
if ($metadata.Os -ne 'linux' -or $metadata.Architecture -ne 'amd64') {
    throw 'The transfer image must be linux/amd64.'
}
$tar = Join-Path $output 'novelty-lens-image.tar'
Invoke-Docker -Arguments @('save','--output',$tar,$Tag)
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-Aramco.ps1') -Destination $output
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'runtime.env.example') -Destination $output
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README-AR.md') -Destination $output
$hash = (Get-FileHash -LiteralPath $tar -Algorithm SHA256).Hash
[IO.File]::WriteAllText((Join-Path $output 'image.sha256'), $hash)
$manifest = @{tag=$Tag; id=$metadata.Id; platform='linux/amd64';
              createdUtc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $output 'image.json'),$manifest)
# Stream large TAR files into a ZIP (including Zip64 when necessary).
# Use an explicit allowlist: runtime.env / host credentials / data never ship.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipPath = Join-Path $repo '.artifacts\NoveltyLens-Aramco.zip'
$zipStream = [IO.File]::Open($zipPath, [IO.FileMode]::Create)
$zip = [IO.Compression.ZipArchive]::new($zipStream, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($name in @('novelty-lens-image.tar','Start-Aramco.ps1',
                        'runtime.env.example','README-AR.md','image.sha256','image.json')) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,(Join-Path $output $name),$name,[IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally { $zip.Dispose() }
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
[IO.File]::WriteAllText("$zipPath.sha256",$zipHash)
Write-Host "Transfer ready: $zipPath"
Get-Item -LiteralPath $zipPath | Select-Object FullName,Length
