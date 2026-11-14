#requires -Version 5.1
# Windows PowerShell 5.1 / PowerShell 7でそれぞれ実行する、合成入力だけの検証。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'scripts\upload_sessions.ps1'
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -ne 0) { throw 'PowerShell構文解析でエラーが検出されました。' }
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('copilot-日本語 空白-' + [Guid]::NewGuid().ToString('N'))
try {
    & python (Join-Path $root 'scripts\demo_offline.py') --out $testRoot
    if ($LASTEXITCODE -ne 0) { throw '合成データ作成に失敗しました。' }
    $staging = Join-Path $testRoot 'staging'
    $manifestFile = Get-ChildItem -LiteralPath $staging -Filter '*.manifest.json' | Select-Object -First 1
    $manifest = Get-Content -LiteralPath $manifestFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    & python (Join-Path $root 'scripts\review_session.py') --staging $staging --fingerprint $manifest.session_fingerprint --reviewer synthetic-test --classification synthetic --account-id 123456789012 --bucket synthetic-test-bucket --approve --confirm-internal-policy
    if ($LASTEXITCODE -ne 0) { throw '合成データの確認記録作成に失敗しました。' }
    & $target -StagingDirectory $staging -Bucket synthetic-test-bucket -ExpectedAwsAccountId 123456789012 -Region ap-northeast-1 -KmsKeyId 'arn:aws:kms:ap-northeast-1:123456789012:key/synthetic-test'
    Write-Output ('PASS: parser + synthetic dry-run; PowerShell=' + $PSVersionTable.PSVersion.ToString())
} finally {
    # このテストが作成した一時ディレクトリだけを削除する。
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
