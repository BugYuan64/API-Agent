param([string]$Version = '1.82.0')
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'This helper supports Windows x64 only; configure POSTMAN_CLI_PATH on other platforms.'
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Use an exact numeric version.' }
$projectRoot = Split-Path $PSScriptRoot -Parent
$destination = Join-Path $projectRoot '.cache/tools/postman'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$metadata = Invoke-RestMethod "https://registry.npmjs.org/@postman%2fpm-bin-windows-x64/$Version"
if (-not $metadata.dist.tarball.StartsWith('https://registry.npmjs.org/@postman/')) {
    throw 'Unexpected package source.'
}
$archive = Join-Path $destination "package-$Version.tgz"
if (-not (Test-Path -LiteralPath $archive)) { Invoke-WebRequest $metadata.dist.tarball -OutFile $archive }
$archiveBytes = [System.IO.File]::ReadAllBytes($archive)
$digest = [Convert]::ToBase64String([System.Security.Cryptography.SHA512]::HashData($archiveBytes))
if ("sha512-$digest" -cne $metadata.dist.integrity) { throw 'Package integrity mismatch.' }
$sourceStream = [System.IO.File]::OpenRead($archive)
$gzipStream = [System.IO.Compression.GZipStream]::new($sourceStream, [System.IO.Compression.CompressionMode]::Decompress)
$reader = [System.Formats.Tar.TarReader]::new($gzipStream)
$binary = Join-Path $destination 'postman.exe'
$found = $false
try {
    while ($null -ne ($entry = $reader.GetNextEntry())) {
        if ($entry.Name.StartsWith('package/bin/') -and $null -ne $entry.DataStream) {
            $relative = $entry.Name.Substring('package/bin/'.Length)
            if ($relative.Contains('\') -or $relative.Split('/') -contains '..' -or [System.IO.Path]::IsPathRooted($relative)) {
                throw 'Unsafe archive entry.'
            }
            if ($entry.EntryType -notin @([System.Formats.Tar.TarEntryType]::RegularFile, [System.Formats.Tar.TarEntryType]::V7RegularFile)) {
                throw 'Unsupported archive entry.'
            }
            $outputPath = Join-Path $destination $relative
            New-Item -ItemType Directory -Path (Split-Path $outputPath -Parent) -Force | Out-Null
            $outputStream = [System.IO.File]::Create($outputPath)
            try { $entry.DataStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
            if ($relative -eq 'postman.exe') { $found = $true }
        }
    }
} finally {
    $reader.Dispose()
    $gzipStream.Dispose()
    $sourceStream.Dispose()
}
if (-not $found) { throw 'Official package has no expected binary.' }
@{ version = $Version; source = $metadata.dist.tarball; integrity = $metadata.dist.integrity } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'installation.json') -Encoding utf8
Remove-Item -LiteralPath $archive
& $binary --version
if ($LASTEXITCODE -ne 0) { throw 'Postman CLI version check failed.' }
Write-Output "Project CLI: $binary"
