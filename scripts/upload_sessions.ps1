#requires -Version 5.1
<#
.SYNOPSIS
  レビュー済みのCopilotセッションZIPを、明示的な許可がある場合だけS3へ保存する。
.DESCRIPTION
  既定はドライラン。-Upload と -ConfirmApprovedData の両方がある場合に限り通信する。
  AWS CLI v2が必要。会社の情報分類・S3送信許可をこのスクリプトは代替しない。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StagingDirectory,
    [Parameter(Mandatory = $true)][string]$Bucket,
    [Parameter(Mandatory = $true)][string]$ExpectedAwsAccountId,
    [Parameter(Mandatory = $true)][string]$Region,
    [Parameter(Mandatory = $true)][string]$KmsKeyId,
    [string]$Prefix = 'copilot-learning/v1',
    [string]$Profile = '',
    [switch]$Upload,
    [switch]$ConfirmApprovedData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($ExpectedAwsAccountId -notmatch '^\d{12}$') {
    throw 'ExpectedAwsAccountId は12桁のAWSアカウントIDを指定してください。'
}
if ($Bucket -notmatch '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$') {
    throw 'Bucket の形式が不正です。'
}
if ($Prefix -notmatch '^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$' -or $Prefix.Contains('..') -or $Prefix.Contains('//')) {
    throw 'Prefix は安全なS3プレフィックスを指定してください。'
}
if ($Upload -and -not $ConfirmApprovedData) {
    throw 'アップロードするには、データの確認と社内承認後に -ConfirmApprovedData を指定してください。'
}
if (-not (Test-Path -LiteralPath $StagingDirectory -PathType Container)) {
    throw 'StagingDirectory が存在しません。'
}

$manifests = @(Get-ChildItem -LiteralPath $StagingDirectory -Filter 'session-*.manifest.json' -File)
if ($manifests.Count -eq 0) {
    throw '送信対象のmanifestがありません。まず prepare_sessions.py を実行してください。'
}

# S3送信の前に全ファイルの整合性を検査する。失敗があれば一件も送信しない。
$packages = @()
foreach ($manifestFile in $manifests) {
    $manifest = Get-Content -LiteralPath $manifestFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne '1.0' -or $manifest.record_type -ne 'copilot-session-archive') {
        throw "スキーマが不正です: $($manifestFile.Name)"
    }
    if ($manifest.review_status -ne 'requires_manual_approval') {
        throw "review_status が不正です: $($manifestFile.Name)"
    }
    if ($manifest.session_fingerprint -notmatch '^[a-f0-9]{64}$') {
        throw "フィンガープリントが不正です: $($manifestFile.Name)"
    }
    $expectedName = "session-$($manifest.session_fingerprint).zip"
    if ($manifest.archive_file -ne $expectedName -or $manifestFile.Name -ne "session-$($manifest.session_fingerprint).manifest.json") {
        throw "ファイル名がmanifestと一致しません: $($manifestFile.Name)"
    }
    $archivePath = Join-Path $StagingDirectory $expectedName
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "ZIPが見つかりません: $expectedName"
    }
    $actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $manifest.archive_sha256) {
        throw "ZIPのSHA-256不一致: $expectedName"
    }
    # チェックボックスだけでなく、送信先とハッシュに紐づく承認記録を要求する。
    # これは社内の実際の情報管理承認そのものを代替するものではない。
    $approvalPath = Join-Path $StagingDirectory "session-$($manifest.session_fingerprint).approval.json"
    if (-not (Test-Path -LiteralPath $approvalPath -PathType Leaf)) {
        throw "承認記録がありません: $($manifest.session_fingerprint)"
    }
    $approval = Get-Content -LiteralPath $approvalPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $manifestSha = (Get-FileHash -LiteralPath $manifestFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($approval.schema_version -ne '1.0' -or
        $approval.record_type -ne 'upload-approval' -or
        $approval.status -ne 'approved' -or
        $approval.session_fingerprint -ne $manifest.session_fingerprint -or
        $approval.archive_sha256 -ne $actualSha256 -or
        $approval.manifest_sha256 -ne $manifestSha -or
        $approval.target_account_id -ne $ExpectedAwsAccountId -or
        $approval.target_bucket -ne $Bucket -or
        [string]::IsNullOrWhiteSpace([string]$approval.reviewer) -or
        [string]::IsNullOrWhiteSpace([string]$approval.reviewed_at_utc) -or
        $approval.data_classification -notin @('synthetic', 'approved_non_sensitive')) {
        throw "承認内容・対象ハッシュ・送信先が不一致: $($manifestFile.Name)"
    }
    $packages += [PSCustomObject]@{
        ArchivePath  = (Resolve-Path -LiteralPath $archivePath).Path
        ManifestPath = $manifestFile.FullName
        ApprovalPath = $approvalPath
        Fingerprint  = [string]$manifest.session_fingerprint
        ArchiveHash  = $actualSha256
    }
}

$commonArgs = @('--no-cli-pager', '--region', $Region)
if (-not [string]::IsNullOrWhiteSpace($Profile)) {
    $commonArgs += @('--profile', $Profile)
}

if (-not $Upload) {
    foreach ($package in $packages) {
        Write-Host "DRY-RUN: $($package.ArchivePath) => s3://$Bucket/$Prefix/raw-sessions/$($package.Fingerprint)/session.zip"
    }
    Write-Host "ドライラン完了: $($packages.Count)件。S3送信は行っていません。"
    return
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw 'AWS CLIが見つかりません。AWS CLI v2をインストールしてください。'
}

# 誤ったAWSアカウントへの送信を防止する。
$identityOutput = & aws sts get-caller-identity --output json @commonArgs
if ($LASTEXITCODE -ne 0) {
    throw 'AWS認証の確認に失敗しました。AWS SSO/プロファイルを確認してください。'
}
$actualAccountId = ($identityOutput | Out-String | ConvertFrom-Json).Account
if ($actualAccountId -ne $ExpectedAwsAccountId) {
    throw "AWSアカウント不一致: expected=$ExpectedAwsAccountId actual=$actualAccountId"
}

function Send-ObjectIfMissing {
    param([string]$SourcePath, [string]$Key, [string]$ExpectedHash)

    $headArguments = @('s3api', 'head-object', '--bucket', $Bucket, '--key', $Key, '--output', 'json') + $commonArgs
    $headOutput = & aws @headArguments 2>$null
    if ($LASTEXITCODE -eq 0) {
        $head = $headOutput | Out-String | ConvertFrom-Json
        if ($head.Metadata.sha256 -eq $ExpectedHash) {
            Write-Host "SKIP（同じSHA）: s3://$Bucket/$Key"
            return
        }
        throw "既存オブジェクトのハッシュが異なります。上書き禁止: s3://$Bucket/$Key"
    }

    # If-None-Match により同じキーを上書きしない（競合時はエラーで停止）。
    $putArguments = @(
        's3api', 'put-object', '--bucket', $Bucket, '--key', $Key,
        '--body', $SourcePath, '--if-none-match', '*',
        '--server-side-encryption', 'aws:kms', '--ssekms-key-id', $KmsKeyId,
        '--metadata', "sha256=$ExpectedHash", '--output', 'json'
    ) + $commonArgs
    & aws @putArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "アップロード失敗（既存オブジェクト/権限/ネットワークを確認）: s3://$Bucket/$Key"
    }
    Write-Host "UPLOADED: s3://$Bucket/$Key"
}

foreach ($package in $packages) {
    $keyBase = "$Prefix/raw-sessions/$($package.Fingerprint)"
    Send-ObjectIfMissing -SourcePath $package.ArchivePath -Key "$keyBase/session.zip" -ExpectedHash $package.ArchiveHash
    $manifestHash = (Get-FileHash -LiteralPath $package.ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Send-ObjectIfMissing -SourcePath $package.ManifestPath -Key "$keyBase/manifest.json" -ExpectedHash $manifestHash
    $approvalHash = (Get-FileHash -LiteralPath $package.ApprovalPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Send-ObjectIfMissing -SourcePath $package.ApprovalPath -Key "$keyBase/approval.json" -ExpectedHash $approvalHash
}
Write-Host "アップロード処理完了: $($packages.Count)件（Memoryへの投入は行っていません）。"
