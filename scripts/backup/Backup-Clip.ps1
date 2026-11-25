[CmdletBinding(DefaultParameterSetName = "Preflight")]
param(
    [Parameter(ParameterSetName = "Preflight")]
    [switch]$Preflight,

    [Parameter(Mandatory = $true, ParameterSetName = "Backup")]
    [switch]$Backup,

    [string]$BackupBase = (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "AI-Agent-Clip-Backups"),
    [string]$OpenSearchRepository = "clip_backup",
    [string]$PostgresContainer = "rag-postgres-1",
    [string]$OpenSearchContainer = "rag-opensearch-1"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ExpectedDatabase = "clip_agent_2026"
$ExpectedCollection = "clip_agent_dense_v1"
$ExpectedIndex = "clip_agent_sparse_v1"
$ExpectedTables = @("documents", "chunks", "search_traces", "trace_feedback")
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$EnvFile = Join-Path $RepoRoot ".env"
$Script:BackupRoot = $null
$Script:Manifest = $null

function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing .env file: $Path"
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
    }
    return $values
}

function Get-RequiredValue {
    param([hashtable]$Values, [string]$Name)

    if (-not $Values.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace($Values[$Name])) {
        throw "Required setting is missing from .env: $Name"
    }
    return [string]$Values[$Name]
}

function Assert-SafeName {
    param([string]$Value, [string]$Label)

    if ($Value -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "$Label contains unsupported characters."
    }
}

function ConvertTo-CanonicalPath {
    param([string]$Path, [string]$BasePath)

    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    }
    return [IO.Path]::GetFullPath((Join-Path $BasePath $Path)).TrimEnd('\', '/')
}

function Test-IsWithinPath {
    param([string]$Candidate, [string]$Parent)

    $candidatePath = [IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return $candidatePath.StartsWith($parentPath, [StringComparison]::OrdinalIgnoreCase)
}

function Invoke-JsonRequest {
    param(
        [ValidateSet("Get", "Post", "Put")][string]$Method,
        [string]$Uri,
        [object]$Body = $null,
        [int]$TimeoutSec = 60
    )

    $parameters = @{
        Method = $Method
        Uri = $Uri
        UseBasicParsing = $true
        TimeoutSec = $TimeoutSec
    }
    if ($null -ne $Body) {
        $parameters["ContentType"] = "application/json"
        $parameters["Body"] = ($Body | ConvertTo-Json -Compress -Depth 8)
    }
    $response = Invoke-WebRequest @parameters
    if ([string]::IsNullOrWhiteSpace($response.Content)) { return $null }
    return ($response.Content | ConvertFrom-Json)
}

function Invoke-DockerText {
    param([string[]]$Arguments)

    $output = & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed (exit $LASTEXITCODE)."
    }
    return @($output)
}

function Get-ListenerCount {
    param([int]$Port)

    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return -1 }
    return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count
}

function Get-DirectoryStats {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return [ordered]@{ file_count = 0; bytes = 0 }
    }
    $files = @(Get-ChildItem -LiteralPath $Path -File -Recurse -Force)
    $bytes = 0L
    foreach ($file in $files) { $bytes += $file.Length }
    return [ordered]@{ file_count = $files.Count; bytes = $bytes }
}

function Get-FreeBytes {
    param([string]$Path)

    $probe = $Path
    while (-not (Test-Path -LiteralPath $probe)) {
        $parent = Split-Path -Parent $probe
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $probe) {
            throw "Cannot resolve an existing parent for backup path: $Path"
        }
        $probe = $parent
    }
    $root = [IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $probe).Path)
    return ([IO.DriveInfo]::new($root)).AvailableFreeSpace
}

function Get-PostgresInfo {
    param([string]$Container, [string]$User, [string]$Database)

    Assert-SafeName $Container "PostgreSQL container"
    Assert-SafeName $User "PostgreSQL user"
    Assert-SafeName $Database "PostgreSQL database"
    $sql = @"
SELECT current_database(),
       current_setting('server_version'),
       pg_database_size(current_database()),
       (SELECT count(*) FROM documents WHERE status = 'indexing'),
       to_regclass('public.documents') IS NOT NULL,
       to_regclass('public.chunks') IS NOT NULL,
       to_regclass('public.search_traces') IS NOT NULL,
       to_regclass('public.trace_feedback') IS NOT NULL;
"@
    $lines = @(Invoke-DockerText @("exec", $Container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", $User, "-d", $Database, "-At", "-F", "|", "-c", $sql))
    if ($lines.Count -ne 1) { throw "Unexpected PostgreSQL preflight response." }
    $parts = $lines[0].Split('|')
    if ($parts.Count -ne 8) { throw "Incomplete PostgreSQL preflight response." }
    return [ordered]@{
        database = $parts[0]
        server_version = $parts[1]
        database_bytes = [int64]$parts[2]
        indexing_documents = [int]$parts[3]
        expected_tables_present = -not ($parts[4..7] -contains "f")
    }
}

function Get-DockerToolVersion {
    param([string]$Container, [string]$Tool)

    $lines = @(Invoke-DockerText @("exec", $Container, $Tool, "--version"))
    return ($lines -join " ").Trim()
}

function ConvertFrom-DockerBindSource {
    param([string]$Source)

    if ([string]::IsNullOrWhiteSpace($Source)) {
        throw "Docker bind source path is empty."
    }
    if ($Source -match '^[A-Za-z]:[\\/]') {
        return [IO.Path]::GetFullPath($Source)
    }
    if ($Source -match '^/run/desktop/mnt/host/([A-Za-z])(?:/(.*))?$') {
        $drive = $Matches[1].ToUpperInvariant()
        $tail = [string]$Matches[2]
        if ([string]::IsNullOrWhiteSpace($tail)) { return "$drive`:" + '\' }
        if ($tail -match '(^|/)\.\.?(/|$)') {
            throw "Docker Desktop bind source contains a relative path segment."
        }
        return [IO.Path]::GetFullPath(("{0}:\{1}" -f $drive, ($tail -replace '/', '\')))
    }
    throw "Unsupported Docker bind source path format: $Source"
}

function ConvertFrom-DockerMountJson {
    param([string]$Json)

    if ([string]::IsNullOrWhiteSpace($Json)) {
        throw "Docker mount JSON is empty."
    }
    try {
        $parsedMounts = ConvertFrom-Json -InputObject $Json -ErrorAction Stop
    } catch {
        throw "Docker mount JSON is invalid."
    }

    $mounts = @()
    foreach ($mount in $parsedMounts) {
        if ($null -eq $mount) {
            throw "Docker mount JSON contains an empty mount."
        }
        foreach ($requiredProperty in @("Type", "Source", "Destination")) {
            if ($null -eq $mount.PSObject.Properties[$requiredProperty]) {
                throw "Docker mount JSON is missing '$requiredProperty'."
            }
        }
        $mounts += $mount
    }
    if ($mounts.Count -eq 0) {
        throw "Docker mount JSON contains no mounts."
    }
    return $mounts
}

function Get-DockerBindMountForDestination {
    param([object[]]$Mounts, [string]$Destination)

    $expectedDestination = $Destination.TrimEnd('/')
    if ([string]::IsNullOrWhiteSpace($expectedDestination)) {
        throw "Expected Docker mount destination is empty."
    }

    $matchingMounts = @()
    foreach ($mount in $Mounts) {
        $mountDestination = ([string]$mount.Destination).TrimEnd('/')
        if ($mountDestination -ceq $expectedDestination) {
            $matchingMounts += $mount
        }
    }
    if ($matchingMounts.Count -eq 0) {
        throw "Repository location '$Destination' is not backed by a visible Docker mount."
    }
    if ($matchingMounts.Count -ne 1) {
        throw "Repository location '$Destination' has multiple Docker mounts."
    }

    $matchingMount = $matchingMounts[0]
    if ([string]$matchingMount.Type -cne "bind") {
        throw "Repository must use a host bind mount, not Docker mount type '$($matchingMount.Type)'."
    }
    return $matchingMount
}

function Get-OpenSearchRepositoryInfo {
    param([string]$BaseUrl, [string]$Repository, [string]$Container)

    $encodedRepository = [Uri]::EscapeDataString($Repository)
    try {
        $repositoryResult = Invoke-JsonRequest -Method Get -Uri "$BaseUrl/_snapshot/$encodedRepository"
    } catch {
        throw "OpenSearch snapshot repository '$Repository' is unavailable. Configure it only after owner approval."
    }
    $property = $repositoryResult.PSObject.Properties[$Repository]
    if ($null -eq $property) { throw "OpenSearch did not return repository '$Repository'." }
    $repositoryConfig = $property.Value
    if ($repositoryConfig.type -ne "fs") {
        throw "Verified automation currently requires an fs OpenSearch repository; found '$($repositoryConfig.type)'."
    }
    $location = [string]$repositoryConfig.settings.location
    if ([string]::IsNullOrWhiteSpace($location) -or -not $location.StartsWith('/')) {
        throw "OpenSearch fs repository has no absolute location."
    }

    $mountJson = (Invoke-DockerText @("inspect", "--format", "{{json .Mounts}}", $Container)) -join ""
    $mounts = @(ConvertFrom-DockerMountJson $mountJson)
    $matchingMount = Get-DockerBindMountForDestination -Mounts $mounts -Destination $location
    $hostPath = ConvertFrom-DockerBindSource ([string]$matchingMount.Source)
    $hostPath = [IO.Path]::GetFullPath($hostPath).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $hostPath -PathType Container)) {
        throw "OpenSearch repository host path is not accessible: $hostPath"
    }
    return [ordered]@{
        name = $Repository
        type = [string]$repositoryConfig.type
        container_location = $location
        host_path = $hostPath
    }
}

function Invoke-DockerDump {
    param([string]$Container, [string]$User, [string]$Database, [string]$OutputPath)

    $docker = (Get-Command docker -ErrorAction Stop).Source
    $processInfo = New-Object Diagnostics.ProcessStartInfo
    $processInfo.FileName = $docker
    $processInfo.Arguments = "exec $Container pg_dump -U $User -d $Database --format=custom"
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $processInfo
    if (-not $process.Start()) { throw "Could not start pg_dump through Docker." }
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $file = [IO.File]::Create($OutputPath)
    try {
        $process.StandardOutput.BaseStream.CopyTo($file)
    } finally {
        $file.Dispose()
    }
    $process.WaitForExit()
    $stderr = $stderrTask.Result
    if ($process.ExitCode -ne 0) {
        throw "pg_dump failed (exit $($process.ExitCode)): $stderr"
    }
}

function Get-DockerRestoreList {
    param([string]$Container, [string]$DumpPath)

    $docker = (Get-Command docker -ErrorAction Stop).Source
    $processInfo = New-Object Diagnostics.ProcessStartInfo
    $processInfo.FileName = $docker
    $processInfo.Arguments = "exec -i $Container pg_restore --list"
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardInput = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $processInfo
    if (-not $process.Start()) { throw "Could not start pg_restore through Docker." }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $file = [IO.File]::OpenRead($DumpPath)
    try {
        $file.CopyTo($process.StandardInput.BaseStream)
        $process.StandardInput.Close()
    } finally {
        $file.Dispose()
    }
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "pg_restore --list failed (exit $($process.ExitCode)): $($stderrTask.Result)"
    }
    return $stdoutTask.Result
}

function Copy-VerifiedDirectory {
    param([string]$Source, [string]$Destination)

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $sourceFiles = @(Get-ChildItem -LiteralPath $Source -File -Recurse -Force)
    foreach ($sourceFile in $sourceFiles) {
        $relative = $sourceFile.FullName.Substring($Source.TrimEnd('\').Length).TrimStart('\')
        $destinationFile = Join-Path $Destination $relative
        $destinationDirectory = Split-Path -Parent $destinationFile
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile.FullName -Destination $destinationFile -Force
    }
    $destinationFiles = @(Get-ChildItem -LiteralPath $Destination -File -Recurse -Force)
    if ($sourceFiles.Count -ne $destinationFiles.Count) {
        throw "File count mismatch while copying '$Source'."
    }
    $sourceBytes = 0L
    foreach ($sourceFile in $sourceFiles) { $sourceBytes += $sourceFile.Length }
    $destinationBytes = 0L
    foreach ($destinationFile in $destinationFiles) { $destinationBytes += $destinationFile.Length }
    if ($sourceBytes -ne $destinationBytes) { throw "Byte count mismatch while copying '$Source'." }
    foreach ($sourceFile in $sourceFiles) {
        $relative = $sourceFile.FullName.Substring($Source.TrimEnd('\').Length).TrimStart('\')
        $destinationFile = Join-Path $Destination $relative
        $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash
        $destinationHash = (Get-FileHash -LiteralPath $destinationFile -Algorithm SHA256).Hash
        if ($sourceHash -ne $destinationHash) { throw "SHA-256 mismatch for copied file '$relative'." }
    }
    return [ordered]@{ file_count = $sourceFiles.Count; bytes = $sourceBytes }
}

function Get-ArtifactEntries {
    param([string]$Root)

    $entries = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force | Sort-Object FullName)) {
        if ($file.Name -eq "manifest.json") { continue }
        $relative = $file.FullName.Substring($Root.TrimEnd('\').Length).TrimStart('\') -replace '\\', '/'
        $entries += [ordered]@{
            path = $relative
            bytes = $file.Length
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    return $entries
}

function Write-Manifest {
    if ($null -eq $Script:BackupRoot -or $null -eq $Script:Manifest) { return }
    $Script:Manifest.files = @(Get-ArtifactEntries $Script:BackupRoot)
    $path = Join-Path $Script:BackupRoot "manifest.json"
    $Script:Manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding UTF8
}

$settings = Read-DotEnv $EnvFile
$postgresDsn = Get-RequiredValue $settings "POSTGRES_DSN"
$qdrantUrl = (Get-RequiredValue $settings "QDRANT_URL").TrimEnd('/')
$qdrantCollection = Get-RequiredValue $settings "QDRANT_COLLECTION"
$openSearchUrl = (Get-RequiredValue $settings "OPENSEARCH_URL").TrimEnd('/')
$openSearchIndex = Get-RequiredValue $settings "OPENSEARCH_INDEX"
$storageSetting = Get-RequiredValue $settings "STORAGE_ROOT"

$normalizedDsn = $postgresDsn -replace '^postgresql\+psycopg://', 'postgresql://'
try { $dsnUri = [Uri]$normalizedDsn } catch { throw "POSTGRES_DSN is not a valid PostgreSQL URI." }
$database = $dsnUri.AbsolutePath.TrimStart('/')
$postgresUser = [Uri]::UnescapeDataString(($dsnUri.UserInfo -split ':', 2)[0])
if ($database -ne $ExpectedDatabase) { throw "Refusing database '$database'; expected '$ExpectedDatabase'." }
if ($qdrantCollection -ne $ExpectedCollection) { throw "Refusing Qdrant collection '$qdrantCollection'." }
if ($openSearchIndex -ne $ExpectedIndex) { throw "Refusing OpenSearch index '$openSearchIndex'." }
if ($dsnUri.Host -notin @("127.0.0.1", "localhost")) { throw "PostgreSQL must use a loopback host." }
foreach ($serviceUrl in @($qdrantUrl, $openSearchUrl)) {
    $serviceUri = [Uri]$serviceUrl
    if ($serviceUri.Host -notin @("127.0.0.1", "localhost")) { throw "Store URLs must use loopback hosts." }
}

$storageRoot = ConvertTo-CanonicalPath $storageSetting $RepoRoot
$backupBasePath = ConvertTo-CanonicalPath $BackupBase $RepoRoot
if (-not (Test-Path -LiteralPath $storageRoot -PathType Container)) { throw "STORAGE_ROOT does not exist." }
if (Test-IsWithinPath $backupBasePath $RepoRoot) { throw "BackupBase must be outside the Git repository." }
if (Test-IsWithinPath $backupBasePath $storageRoot) { throw "BackupBase must not be inside STORAGE_ROOT." }
if (Test-IsWithinPath $storageRoot $backupBasePath) { throw "STORAGE_ROOT must not be inside BackupBase." }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI is required." }
$null = Invoke-DockerText @("inspect", $PostgresContainer)
$null = Invoke-DockerText @("inspect", $OpenSearchContainer)
$postgresInfo = Get-PostgresInfo $PostgresContainer $postgresUser $database
if ($postgresInfo.database -ne $ExpectedDatabase) { throw "PostgreSQL returned the wrong database." }
if (-not $postgresInfo.expected_tables_present) { throw "One or more expected PostgreSQL tables are missing." }
if ($postgresInfo.indexing_documents -ne 0) { throw "Active document indexing detected; backup is blocked." }
$pgDumpVersion = Get-DockerToolVersion $PostgresContainer "pg_dump"
$pgRestoreVersion = Get-DockerToolVersion $PostgresContainer "pg_restore"
if ($pgDumpVersion -notmatch ' 17\.') { throw "PostgreSQL 17 pg_dump is required; found: $pgDumpVersion" }

$qdrantInfo = Invoke-JsonRequest -Method Get -Uri "$qdrantUrl/collections/$([Uri]::EscapeDataString($qdrantCollection))"
if ($qdrantInfo.status -ne "ok" -or $qdrantInfo.result.status -ne "green") {
    throw "Qdrant collection is not healthy."
}
$qdrantVersion = (Invoke-JsonRequest -Method Get -Uri $qdrantUrl).version
$openSearchInfo = Invoke-JsonRequest -Method Get -Uri $openSearchUrl
$encodedIndex = [Uri]::EscapeDataString($openSearchIndex)
$indexInfo = Invoke-JsonRequest -Method Get -Uri "$openSearchUrl/_cat/indices/$encodedIndex`?format=json&h=index,health,status,docs.count,store.size"
if (@($indexInfo).Count -ne 1 -or $indexInfo[0].index -ne $ExpectedIndex -or $indexInfo[0].status -ne "open") {
    throw "OpenSearch index is unavailable or ambiguous."
}
$activeSnapshots = Invoke-JsonRequest -Method Get -Uri "$openSearchUrl/_snapshot/_status"
if (@($activeSnapshots.snapshots).Count -ne 0) { throw "Another OpenSearch snapshot operation is active." }
$repositoryInfo = Get-OpenSearchRepositoryInfo $openSearchUrl $OpenSearchRepository $OpenSearchContainer
if (Test-IsWithinPath $repositoryInfo.host_path $RepoRoot) {
    throw "OpenSearch repository must be outside the Git repository."
}
if (Test-IsWithinPath $backupBasePath $repositoryInfo.host_path) {
    throw "BackupBase must not be nested inside the live OpenSearch repository."
}

$backendPort = if ($settings.ContainsKey("DEMO_BACKEND_PORT")) { [int]$settings["DEMO_BACKEND_PORT"] } else { 8000 }
$proxyPort = if ($settings.ContainsKey("DEMO_PROXY_PORT")) { [int]$settings["DEMO_PROXY_PORT"] } else { 8080 }
$backendListeners = Get-ListenerCount $backendPort
$proxyListeners = Get-ListenerCount $proxyPort
if ($Backup -and ($backendListeners -gt 0 -or $proxyListeners -gt 0)) {
    throw "FastAPI/Caddy listener detected. Stop writes manually; the backup script never stops processes."
}

$storageStats = Get-DirectoryStats $storageRoot
$repositoryStats = Get-DirectoryStats $repositoryInfo.host_path
if ($storageStats.file_count -eq 0) { throw "STORAGE_ROOT is empty; verified backup is blocked." }
$estimatedBytes = [int64]$postgresInfo.database_bytes + [int64]$storageStats.bytes + [int64]$repositoryStats.bytes
$requiredFreeBytes = [Math]::Max(1GB, [int64]($estimatedBytes * 2))
$freeBytes = Get-FreeBytes $backupBasePath
if ($freeBytes -lt $requiredFreeBytes) {
    throw "Insufficient free space at BackupBase: $freeBytes bytes available, $requiredFreeBytes required."
}

Write-Host "CLIP-009 preflight passed."
Write-Host "Resources: PostgreSQL/$ExpectedDatabase, Qdrant/$ExpectedCollection, OpenSearch/$ExpectedIndex."
Write-Host "PostgreSQL: $($postgresInfo.server_version); pg_dump: $pgDumpVersion"
Write-Host "Qdrant: $qdrantVersion; OpenSearch: $($openSearchInfo.version.number)"
Write-Host "STORAGE_ROOT files: $($storageStats.file_count); bytes: $($storageStats.bytes)"
Write-Host "Backup free bytes: $freeBytes; conservative minimum: $requiredFreeBytes"
Write-Host "FastAPI/Caddy listeners: $backendListeners/$proxyListeners"
Write-Host "OpenSearch repository: $($repositoryInfo.name); host path: $($repositoryInfo.host_path)"

if (-not $Backup) { return }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Script:BackupRoot = Join-Path $backupBasePath "clip009-$stamp"
New-Item -ItemType Directory -Path $Script:BackupRoot | Out-Null
Set-Content -LiteralPath (Join-Path $Script:BackupRoot ".gitignore") -Value "*" -Encoding ASCII
Set-Content -LiteralPath (Join-Path $Script:BackupRoot "PRIVATE_BACKUP_DO_NOT_PUBLISH.txt") -Value @(
    "Private AI-Agent Clip backup. Do not publish or commit this directory."
    "The .env file and credentials are intentionally excluded."
) -Encoding UTF8

$gitSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
$gitDirty = -not [string]::IsNullOrWhiteSpace((& git -C $RepoRoot status --porcelain) -join "`n")
$Script:Manifest = [ordered]@{
    schema_version = 1
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    git = [ordered]@{ sha = $gitSha; dirty = $gitDirty }
    resources = [ordered]@{
        postgresql = $ExpectedDatabase
        qdrant = $ExpectedCollection
        opensearch = $ExpectedIndex
        storage_root = "configured path recorded locally but omitted from manifest"
    }
    versions = [ordered]@{
        postgresql_server = $postgresInfo.server_version
        pg_dump = $pgDumpVersion
        pg_restore = $pgRestoreVersion
        qdrant = $qdrantVersion
        opensearch = [string]$openSearchInfo.version.number
    }
    consistency = [ordered]@{
        fastapi_listener_count = $backendListeners
        caddy_listener_count = $proxyListeners
        indexing_documents_before = $postgresInfo.indexing_documents
        boundary = "Sequential application-consistent backup; not one atomic transaction across four stores."
    }
    components = [ordered]@{
        postgresql = [ordered]@{ status = "not_run" }
        qdrant = [ordered]@{ status = "not_run" }
        storage = [ordered]@{ status = "not_run" }
        opensearch = [ordered]@{ status = "not_run" }
    }
    files = @()
    complete = $false
    limitations = @(
        "The four stores do not support a shared atomic snapshot boundary."
        "A successful archive/list/checksum verification is not a restore test."
        "No restore was performed against production resources."
    )
    errors = @()
}

try {
    $dumpPath = Join-Path $Script:BackupRoot "$ExpectedDatabase.dump"
    Invoke-DockerDump $PostgresContainer $postgresUser $database $dumpPath
    if (-not (Test-Path -LiteralPath $dumpPath -PathType Leaf) -or (Get-Item $dumpPath).Length -le 0) {
        throw "PostgreSQL dump is empty."
    }
    $restoreList = Get-DockerRestoreList $PostgresContainer $dumpPath
    foreach ($table in $ExpectedTables) {
        if ($restoreList -notmatch "(?m) TABLE (DATA )?public $([regex]::Escape($table)) ") {
            throw "Expected table '$table' is missing from pg_restore --list."
        }
    }
    $Script:Manifest.components.postgresql = [ordered]@{
        status = "verified"
        format = "custom"
        archive_list_read = $true
        expected_tables = $ExpectedTables
    }
    Write-Manifest

    $encodedCollection = [Uri]::EscapeDataString($qdrantCollection)
    $snapshotResponse = Invoke-JsonRequest -Method Post -Uri "$qdrantUrl/collections/$encodedCollection/snapshots?wait=true" -TimeoutSec 300
    if ($snapshotResponse.status -ne "ok" -or [string]::IsNullOrWhiteSpace($snapshotResponse.result.name)) {
        throw "Qdrant snapshot creation failed."
    }
    $qdrantSnapshotName = [string]$snapshotResponse.result.name
    Assert-SafeName $qdrantSnapshotName "Qdrant snapshot name"
    $qdrantSnapshotPath = Join-Path $Script:BackupRoot $qdrantSnapshotName
    Invoke-WebRequest -Uri "$qdrantUrl/collections/$encodedCollection/snapshots/$([Uri]::EscapeDataString($qdrantSnapshotName))" `
        -OutFile $qdrantSnapshotPath -UseBasicParsing -TimeoutSec 300
    $snapshotList = Invoke-JsonRequest -Method Get -Uri "$qdrantUrl/collections/$encodedCollection/snapshots"
    $listedSnapshot = @($snapshotList.result | Where-Object { $_.name -eq $qdrantSnapshotName })
    if ($listedSnapshot.Count -ne 1 -or (Get-Item $qdrantSnapshotPath).Length -le 0) {
        throw "Downloaded Qdrant snapshot could not be verified."
    }
    $Script:Manifest.components.qdrant = [ordered]@{
        status = "verified"
        snapshot_name = $qdrantSnapshotName
        api_metadata_read = $true
    }
    Write-Manifest

    $storageDestination = Join-Path $Script:BackupRoot "storage"
    $storageCopy = Copy-VerifiedDirectory $storageRoot $storageDestination
    $Script:Manifest.components.storage = [ordered]@{
        status = "verified"
        file_count = $storageCopy.file_count
        bytes = $storageCopy.bytes
        source_and_copy_sha256_compared = $true
    }
    Write-Manifest

    $snapshotName = "clip009-$stamp"
    $snapshotBody = [ordered]@{ indices = $ExpectedIndex; include_global_state = $false }
    $snapshotResult = Invoke-JsonRequest -Method Put `
        -Uri "$openSearchUrl/_snapshot/$([Uri]::EscapeDataString($OpenSearchRepository))/$snapshotName`?wait_for_completion=true" `
        -Body $snapshotBody -TimeoutSec 1800
    if ($snapshotResult.snapshot.state -ne "SUCCESS") { throw "OpenSearch snapshot did not finish with SUCCESS." }
    $snapshotCheck = Invoke-JsonRequest -Method Get `
        -Uri "$openSearchUrl/_snapshot/$([Uri]::EscapeDataString($OpenSearchRepository))/$snapshotName"
    if (@($snapshotCheck.snapshots).Count -ne 1 -or $snapshotCheck.snapshots[0].state -ne "SUCCESS") {
        throw "OpenSearch snapshot verification failed."
    }
    $indices = @($snapshotCheck.snapshots[0].indices)
    if ($indices.Count -ne 1 -or $indices[0] -ne $ExpectedIndex) {
        throw "OpenSearch snapshot contains unexpected indices."
    }
    $repositoryDestination = Join-Path $Script:BackupRoot "opensearch-repository"
    $repositoryCopy = Copy-VerifiedDirectory $repositoryInfo.host_path $repositoryDestination
    if ($repositoryCopy.file_count -eq 0) { throw "Copied OpenSearch repository is empty." }
    $Script:Manifest.components.opensearch = [ordered]@{
        status = "verified"
        repository = $repositoryInfo.name
        repository_type = $repositoryInfo.type
        repository_container_location = $repositoryInfo.container_location
        repository_host_path = "omitted from manifest; printed during local preflight"
        snapshot_name = $snapshotName
        snapshot_state = "SUCCESS"
        include_global_state = $false
        indices = $indices
        repository_copy_file_count = $repositoryCopy.file_count
        repository_copy_bytes = $repositoryCopy.bytes
    }

    $postgresAfter = Get-PostgresInfo $PostgresContainer $postgresUser $database
    if ($postgresAfter.indexing_documents -ne 0) { throw "Indexing appeared during backup." }
    $Script:Manifest.consistency.indexing_documents_after = $postgresAfter.indexing_documents
    $Script:Manifest.complete = $true
    Write-Manifest
    Write-Host "Verified backup completed: $Script:BackupRoot"
} catch {
    if ($null -ne $Script:Manifest) {
        $Script:Manifest.errors += $_.Exception.Message
        $Script:Manifest.complete = $false
        Write-Manifest
    }
    throw
}
