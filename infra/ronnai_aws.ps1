<#
=============================================================================
 CS341-RONNAI : ตั้งค่า AWS (Issue #2) ผ่าน AWS CLI — สำหรับ AWS Academy Learner Lab
 ใช้บน Windows (PowerShell 5.1 หรือ PowerShell 7)

   powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action setup
   powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action verify
   powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action teardown

 ปรับค่าได้ เช่น  -Bucket my-bucket -ScanLimitMB 512 -AwsProfile default
 รันซ้ำได้ (idempotent): ของที่มีอยู่แล้วจะข้าม ไม่สร้างซ้ำ
=============================================================================
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('setup', 'verify', 'teardown')]
    [string]$Action,
    [string]$Region = 'us-east-1',          # Learner Lab ใช้ us-east-1
    [string]$AwsProfile = '',               # เว้นว่าง = ใช้ default profile
    [string]$Bucket = '',                   # เว้นว่าง = cs341-ronnai-<account_id>
    [string]$Workgroup = 'ronnai-wg',
    [string]$DbTrusted = 'ronnai_trusted',
    [string]$DbCurated = 'ronnai_curated',
    [ValidateRange(10, 1048576)]
    [int]$ScanLimitMB = 1024,               # จำกัดข้อมูลที่สแกนต่อ query
    [int]$ResultExpireDays = 7              # ลบผล query เก่าใน athena-results/ อัตโนมัติ
)

$ErrorActionPreference = 'Continue'   # ต้องเป็น Continue เพื่อให้ 2>$null กับ aws.exe ไม่ throw ใน PS 5.1
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:AWS_PAGER = ''

$Zones = @('incoming', 'raw', 'trusted', 'curated', 'quarantine', 'athena-results')
$RepoDir = Split-Path -Parent $PSScriptRoot
$SettingsFile = Join-Path $RepoDir 'config\settings.yaml'
$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ronnai-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TmpDir | Out-Null

function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Skip($m) { Write-Host "  [SKIP] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Step($m) { Write-Host ""; Write-Host "== $m ==" -ForegroundColor White }
function Die($m)  { Fail $m; Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue; exit 1 }

# เรียก aws CLI: คืน stdout (string), ตั้ง $script:AwsOk = $true/$false
# (ตั้งใจเป็น simple function ใช้ $args เพื่อให้ส่ง --flag ต่อให้ aws ได้ตรง ๆ)
function Invoke-Aws {
    $AwsArgs = $args
    $common = @('--region', $Region)
    if ($AwsProfile) { $common += @('--profile', $AwsProfile) }
    $errFile = Join-Path $TmpDir 'stderr.txt'
    $out = & aws @common @AwsArgs 2> $errFile
    $script:AwsOk = ($LASTEXITCODE -eq 0)
    $script:AwsErr = if (Test-Path $errFile) { (Get-Content $errFile -Raw) } else { '' }
    if ($out -is [array]) { $out = $out -join "`n" }
    return $out
}

# เขียน JSON ลงไฟล์ (UTF-8 ไม่มี BOM) แล้วคืนค่าเป็น file://... สำหรับส่งให้ aws CLI
# (ส่ง JSON ผ่านไฟล์เพื่อเลี่ยงปัญหา PowerShell ตัดเครื่องหมาย " ทิ้ง)
function JsonFile($name, $obj) {
    $path = Join-Path $TmpDir $name
    $json = $obj | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
    return 'file://' + ($path -replace '\\', '/')
}

function Preflight {
    if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
        Die 'ไม่พบ AWS CLI — ติดตั้ง AWS CLI v2: https://awscli.amazonaws.com/AWSCLIV2.msi แล้วเปิด PowerShell ใหม่'
    }
    Step 'ตรวจ credentials'
    $ident = Invoke-Aws sts get-caller-identity --query '[Account,Arn]' --output text
    if (-not $script:AwsOk) {
        Fail $script:AwsErr
        Die "credentials ใช้ไม่ได้/หมดอายุ — กด Start Lab แล้วคัดลอก AWS CLI credentials จาก 'AWS Details' ไปวางใน %USERPROFILE%\.aws\credentials ใหม่"
    }
    $parts = $ident.Trim() -split '\s+'
    $script:AccountId = $parts[0]
    Ok "Account: $($parts[0])"
    Ok "Identity: $($parts[1])"
    if (-not $script:Bucket) { $script:Bucket = "cs341-ronnai-$($script:AccountId)" }
    Ok "Region: $Region | Bucket: $script:Bucket | Workgroup: $Workgroup"
}

# ---------------------------------------------------------------------------
function Do-Setup {
    Preflight
    $B = $script:Bucket

    Step '1) S3 bucket + zones'
    Invoke-Aws s3api head-bucket --bucket $B | Out-Null
    if ($script:AwsOk) {
        Skip "bucket $B มีอยู่แล้ว"
    } else {
        if ($Region -eq 'us-east-1') {
            Invoke-Aws s3api create-bucket --bucket $B | Out-Null
        } else {
            Invoke-Aws s3api create-bucket --bucket $B --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
        }
        if (-not $script:AwsOk) { Fail $script:AwsErr; Die 'สร้าง bucket ไม่ได้ (ชื่ออาจซ้ำกับคนอื่น ลองใส่ -Bucket ชื่ออื่น)' }
        Invoke-Aws s3api wait bucket-exists --bucket $B | Out-Null
        Ok "สร้าง bucket $B"
    }

    Invoke-Aws s3api put-public-access-block --bucket $B --public-access-block-configuration `
        'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' | Out-Null
    if ($script:AwsOk) { Ok 'ปิด public access' } else { Warn 'ตั้ง public access block ไม่ได้ (Learner Lab อาจไม่อนุญาต) — ข้ามได้' }

    foreach ($z in $Zones) {
        Invoke-Aws s3api put-object --bucket $B --key "$z/" | Out-Null
        if ($script:AwsOk) { Ok "s3://$B/$z/" } else { Fail $script:AwsErr; Die "สร้าง $z/ ไม่ได้" }
    }

    $lifecycle = @{
        Rules = @(
            @{ ID = 'expire-athena-results'; Filter = @{ Prefix = 'athena-results/' }; Status = 'Enabled';
               Expiration = @{ Days = $ResultExpireDays } },
            @{ ID = 'abort-incomplete-uploads'; Filter = @{ Prefix = '' }; Status = 'Enabled';
               AbortIncompleteMultipartUpload = @{ DaysAfterInitiation = 1 } }
        )
    }
    Invoke-Aws s3api put-bucket-lifecycle-configuration --bucket $B `
        --lifecycle-configuration (JsonFile 'lifecycle.json' $lifecycle) | Out-Null
    if ($script:AwsOk) { Ok "lifecycle: ลบ athena-results/ ที่เก่ากว่า $ResultExpireDays วัน" }
    else { Warn 'ตั้ง lifecycle ไม่ได้ — ข้ามได้ ไม่กระทบการทำงาน' }

    Step '2) Glue databases'
    foreach ($pair in @(@($DbTrusted, 'trusted'), @($DbCurated, 'curated'))) {
        $db = $pair[0]; $zone = $pair[1]
        Invoke-Aws glue get-database --name $db | Out-Null
        if ($script:AwsOk) { Skip "Glue database $db มีอยู่แล้ว"; continue }
        $dbInput = @{ Name = $db; Description = "CS341-RONNAI $zone zone"; LocationUri = "s3://$B/$zone/" }
        Invoke-Aws glue create-database --database-input (JsonFile 'db.json' $dbInput) | Out-Null
        if ($script:AwsOk) { Ok "สร้าง Glue database $db -> s3://$B/$zone/" } else { Fail $script:AwsErr; Die "สร้าง $db ไม่ได้" }
    }

    Step "3) Athena workgroup (scan limit $ScanLimitMB MB/query)"
    $cutoff = [int64]$ScanLimitMB * 1024 * 1024
    $output = "s3://$B/athena-results/"
    Invoke-Aws athena get-work-group --work-group $Workgroup | Out-Null
    if ($script:AwsOk) {
        $upd = @{
            ResultConfigurationUpdates      = @{ OutputLocation = $output }
            EnforceWorkGroupConfiguration   = $true
            PublishCloudWatchMetricsEnabled = $false
            BytesScannedCutoffPerQuery      = $cutoff
            EngineVersion                   = @{ SelectedEngineVersion = 'Athena engine version 3' }
        }
        Invoke-Aws athena update-work-group --work-group $Workgroup --state ENABLED `
            --configuration-updates (JsonFile 'wg_update.json' $upd) | Out-Null
        if ($script:AwsOk) { Ok "อัปเดต workgroup $Workgroup" } else { Fail $script:AwsErr; Die 'อัปเดต workgroup ไม่ได้' }
    } else {
        $cfg = @{
            ResultConfiguration             = @{ OutputLocation = $output }
            EnforceWorkGroupConfiguration   = $true
            PublishCloudWatchMetricsEnabled = $false
            BytesScannedCutoffPerQuery      = $cutoff
            EngineVersion                   = @{ SelectedEngineVersion = 'Athena engine version 3' }
        }
        Invoke-Aws athena create-work-group --name $Workgroup --description 'CS341-RONNAI workgroup' `
            --configuration (JsonFile 'wg.json' $cfg) | Out-Null
        if ($script:AwsOk) { Ok "สร้าง workgroup $Workgroup" } else { Fail $script:AwsErr; Die 'สร้าง workgroup ไม่ได้' }
    }
    Ok "ผล query -> $output"

    Step '4) config\settings.yaml'
    Write-Settings

    Write-Host ''
    Write-Host 'เสร็จแล้ว — ต่อด้วย: powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action verify' -ForegroundColor Green
}

function Write-Settings {
    $B = $script:Bucket
    if (Test-Path $SettingsFile) {
        Skip "มี $SettingsFile อยู่แล้ว (ไม่เขียนทับ) — เช็กว่า bucket = $B, region = $Region, workgroup = $Workgroup"
        return
    }
    $prof = if ($AwsProfile) { $AwsProfile } else { 'default' }
    $yaml = @"
# สร้างอัตโนมัติโดย infra/ronnai_aws.ps1 (ไฟล์นี้ถูก .gitignore ไว้ ห้าม commit)
# ห้ามใส่ access key / secret ในไฟล์นี้
aws:
  region: $Region
  profile: $prof
s3:
  bucket: $B
  prefixes:
    incoming: incoming/
    raw: raw/
    trusted: trusted/
    curated: curated/
    quarantine: quarantine/
    athena_results: athena-results/
glue:
  databases:
    trusted: $DbTrusted
    curated: $DbCurated
athena:
  workgroup: $Workgroup
  scan_limit_mb: $ScanLimitMB
"@
    New-Item -ItemType Directory -Force -Path (Split-Path $SettingsFile) | Out-Null
    [System.IO.File]::WriteAllText($SettingsFile, ($yaml -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
    Ok "เขียน $SettingsFile"
}

# ---------------------------------------------------------------------------
function Do-Verify {
    Preflight
    $B = $script:Bucket
    $errors = 0

    Step 'S3 zones'
    $prefixes = Invoke-Aws s3api list-objects-v2 --bucket $B --delimiter / --query 'CommonPrefixes[].Prefix' --output text
    $list = if ($script:AwsOk -and $prefixes) { $prefixes.Trim() -split '\s+' } else { @() }
    foreach ($z in $Zones) {
        if ($list -contains "$z/") { Ok "$z/" } else { Fail "ไม่พบ $z/"; $errors++ }
    }

    Step 'Glue databases'
    foreach ($db in @($DbTrusted, $DbCurated)) {
        Invoke-Aws glue get-database --name $db | Out-Null
        if ($script:AwsOk) { Ok $db } else { Fail "ไม่พบ $db"; $errors++ }
    }

    Step 'Athena workgroup'
    $cutoff = Invoke-Aws athena get-work-group --work-group $Workgroup --query 'WorkGroup.Configuration.BytesScannedCutoffPerQuery' --output text
    if ($script:AwsOk) {
        $c = "$cutoff".Trim()
        if ($c -match '^\d+$') { Ok ("$Workgroup scan limit = {0} MB/query" -f ([int64]$c / 1MB)) }
        else { Fail "$Workgroup ยังไม่ได้ตั้ง scan limit"; $errors++ }
        $out = Invoke-Aws athena get-work-group --work-group $Workgroup --query 'WorkGroup.Configuration.ResultConfiguration.OutputLocation' --output text
        Ok "output = $("$out".Trim())"
    } else {
        Fail "ไม่พบ workgroup $Workgroup"; $errors++
    }

    Step 'Athena: SELECT 1'
    $qid = Invoke-Aws athena start-query-execution --work-group $Workgroup --query-string 'SELECT 1 AS ok' --query QueryExecutionId --output text
    if ($script:AwsOk) {
        $qid = "$qid".Trim()
        $state = ''
        for ($i = 0; $i -lt 30; $i++) {
            $state = ("" + (Invoke-Aws athena get-query-execution --query-execution-id $qid --query QueryExecution.Status.State --output text)).Trim()
            if ($state -in @('SUCCEEDED', 'FAILED', 'CANCELLED')) { break }
            Start-Sleep -Seconds 1
        }
        if ($state -eq 'SUCCEEDED') {
            $val = Invoke-Aws athena get-query-results --query-execution-id $qid --query 'ResultSet.Rows[1].Data[0].VarCharValue' --output text
            Ok "SELECT 1 -> $("$val".Trim()) (query id $qid)"
        } else {
            $reason = Invoke-Aws athena get-query-execution --query-execution-id $qid --query QueryExecution.Status.StateChangeReason --output text
            Fail "query ${state}: $reason"; $errors++
        }
    } else {
        Fail $script:AwsErr; $errors++
    }

    Write-Host ''
    if ($errors -eq 0) { Write-Host 'ผ่านทุกข้อ' -ForegroundColor Green }
    else { Write-Host "มีปัญหา $errors ข้อ — ลองรัน setup ใหม่" -ForegroundColor Red; Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue; exit 1 }
}

# ---------------------------------------------------------------------------
function Do-Teardown {
    Preflight
    $B = $script:Bucket
    Write-Host ''
    Write-Host "จะลบถาวร: bucket s3://$B (รวม raw/ และ quarantine/), Glue DB $DbTrusted $DbCurated, workgroup $Workgroup" -ForegroundColor Red
    $answer = Read-Host 'พิมพ์ชื่อ bucket เพื่อยืนยัน'
    if ($answer -ne $B) { Die 'ยกเลิก (ชื่อไม่ตรง)' }

    Step 'ลบ Athena workgroup'
    Invoke-Aws athena delete-work-group --work-group $Workgroup --recursive-delete-option | Out-Null
    if ($script:AwsOk) { Ok $Workgroup } else { Skip "ไม่พบ $Workgroup" }

    Step 'ลบ Glue databases (รวมตารางข้างใน)'
    foreach ($db in @($DbTrusted, $DbCurated)) {
        Invoke-Aws glue delete-database --name $db | Out-Null
        if ($script:AwsOk) { Ok $db } else { Skip "ไม่พบ $db" }
    }

    Step 'ลบ S3 bucket'
    Invoke-Aws s3api head-bucket --bucket $B | Out-Null
    if ($script:AwsOk) {
        Invoke-Aws s3 rm "s3://$B" --recursive --only-show-errors | Out-Null
        Invoke-Aws s3api delete-bucket --bucket $B | Out-Null
        if ($script:AwsOk) { Ok $B } else { Fail $script:AwsErr }
    } else {
        Skip "ไม่พบ $B"
    }
}

switch ($Action) {
    'setup'    { Do-Setup }
    'verify'   { Do-Verify }
    'teardown' { Do-Teardown }
}
Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
