param([string]$SourceRoot = 'D:\Days\Days-项目\CRM管理系统\CRM阶段2')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$sourceBackend = Join-Path $SourceRoot 'crm-backend'
if (-not (Test-Path -LiteralPath (Join-Path $sourceBackend 'pom.xml'))) { throw 'CRM pom.xml not found.' }
$workspace = Join-Path $projectRoot '.cache/crm-smoke'
New-Item -ItemType Directory -Path $workspace -Force | Out-Null
[xml]$pom = Get-Content -LiteralPath (Join-Path $sourceBackend 'pom.xml') -Raw
$namespace = $pom.DocumentElement.NamespaceURI
$build = $pom.project.build
foreach ($property in @{
    directory = (Join-Path $workspace 'target')
    sourceDirectory = (Join-Path $sourceBackend 'src/main/java')
}.GetEnumerator()) {
    $element = $pom.CreateElement($property.Key, $namespace)
    $element.InnerText = $property.Value.Replace('\', '/')
    $build.AppendChild($element) | Out-Null
}
$resources = $pom.CreateElement('resources', $namespace)
$resource = $pom.CreateElement('resource', $namespace)
$directory = $pom.CreateElement('directory', $namespace)
$directory.InnerText = (Join-Path $sourceBackend 'src/main/resources').Replace('\', '/')
$resource.AppendChild($directory) | Out-Null
$resources.AppendChild($resource) | Out-Null
$build.AppendChild($resources) | Out-Null
$pom.Save((Join-Path $workspace 'pom.xml'))
'<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0" />' |
    Set-Content -LiteralPath (Join-Path $workspace 'settings.xml') -Encoding utf8
Write-Output 'Prepared private build directory .cache/crm-smoke; original CRM sources unchanged.'
