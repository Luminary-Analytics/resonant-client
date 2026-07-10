param(
    [string]$BundleRoot = "dist/resonant",
    [string]$ManifestPath = "dist/bundle-manifest.json",
    [switch]$KeepEnvironment
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bundle = [IO.Path]::GetFullPath((Join-Path $repo $BundleRoot))
$build = [IO.Path]::GetFullPath((Join-Path $repo "build"))
$dist = [IO.Path]::GetFullPath((Join-Path $repo "dist"))
$eggInfo = [IO.Path]::GetFullPath((Join-Path $repo "resonant_client.egg-info"))
$tempRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) "resonant-clean-build-$PID"))
$pushed = $false

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $prefix = $Parent.TrimEnd('\') + '\'
    if (-not $Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing filesystem operation outside $Parent`: $Path"
    }
}

Assert-ChildPath $bundle $repo
Assert-ChildPath $build $repo
Assert-ChildPath $dist $repo
Assert-ChildPath $eggInfo $repo

try {
    Push-Location $repo
    $pushed = $true
    if (Test-Path -LiteralPath $build) { Remove-Item -LiteralPath $build -Recurse -Force }
    if (Test-Path -LiteralPath $bundle) { Remove-Item -LiteralPath $bundle -Recurse -Force }
    if (Test-Path -LiteralPath $eggInfo) { Remove-Item -LiteralPath $eggInfo -Recurse -Force }
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }

    python -m venv $tempRoot
    $python = Join-Path $tempRoot "Scripts/python.exe"
    & $python -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed with exit code $LASTEXITCODE" }
    & $python -m pip install --disable-pip-version-check --no-cache-dir "${repo}[gui,desktop]" pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Build dependency install failed with exit code $LASTEXITCODE" }
    & $python -m PyInstaller (Join-Path $repo "packaging/resonant.spec") --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath (Join-Path $bundle "resonant.exe"))) {
        throw "Clean build did not produce $bundle\resonant.exe"
    }

    & $python (Join-Path $repo "packaging/check_bundle.py") $bundle `
        --policy (Join-Path $repo "packaging/bundle-policy.json") `
        --manifest (Join-Path $repo $ManifestPath)
    if ($LASTEXITCODE -ne 0) { throw "Bundle policy gate failed" }
} finally {
    if ($pushed) { Pop-Location }
    if (Test-Path -LiteralPath $eggInfo) {
        Assert-ChildPath $eggInfo $repo
        Remove-Item -LiteralPath $eggInfo -Recurse -Force
    }
    if (-not $KeepEnvironment -and (Test-Path -LiteralPath $tempRoot)) {
        $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
        $systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        Assert-ChildPath $resolvedTemp $systemTemp
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
