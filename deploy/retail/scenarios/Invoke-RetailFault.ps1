# 首批实验入口：默认只展示命令；只有显式 Inject / Recover 才改变实验环境。
[CmdletBinding()]
param(
    [ValidateSet('R00', 'R01', 'R02', 'R03', 'R04', 'R05')]
    [string]$CaseId = 'R01',
    [ValidateSet('Plan', 'Inspect', 'Inject', 'Recover')]
    [string]$Action = 'Plan',
    [ValidatePattern('^192\.168\.108\.128$')]
    [string]$LabHost = '192.168.108.128',
    [string]$IdentityFile = 'D:\VMs\dotops-ubuntu\id_ed25519',
    [string]$KnownHostsFile = 'D:\VMs\dotops-ubuntu\known_hosts',
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'runs')
)

$ErrorActionPreference = 'Stop'
$dataset = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'cases.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$case = $dataset.cases | Where-Object { $_.id -eq $CaseId } | Select-Object -First 1
if (-not $case) { throw "找不到用例：$CaseId" }

# 仅通过已验证的实验主机访问 Compose 内部服务，不向 Windows 发布额外端口。
function Invoke-LabCommand {
    param([Parameter(Mandatory)][string]$Command)
    # Linux 输出为 UTF-8；Windows PowerShell 的中文默认编码会破坏 JSON 中的转义字符。
    $previousOutputEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $output = & ssh -i $IdentityFile -o "UserKnownHostsFile=$KnownHostsFile" `
            -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=8 `
            -o ServerAliveInterval=10 -o ServerAliveCountMax=2 `
            "dotops@$LabHost" "cd /opt/retail-store && $Command" 2>&1
        $code = $LASTEXITCODE
    } finally {
        # 只在本次 SSH 调用中指定编码，不改变用户后续终端命令的编码。
        [Console]::OutputEncoding = $previousOutputEncoding
    }
    $result = ($output | Out-String).Trim()
    if ($code -ne 0) { throw "远端命令失败，退出码 $code。$result" }
    return $result
}

# 注入前要求所有容器正常，并检查首批实验相关开关和真实商品响应。
function Assert-LabBaseline {
    $lines = Invoke-LabCommand 'docker compose ps --all --format json'
    $containers = @($lines -split '\r?\n' | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
    if ($containers.Count -ne 13) { throw "预期 13 个实验容器，实际 $($containers.Count)；请先 Inspect 核对环境。" }
    foreach ($container in $containers) {
        if ($container.State -ne 'running' -or ($container.Health -and $container.Health -ne 'healthy')) {
            throw "容器未恢复基线：$($container.Service) $($container.State) $($container.Health)"
        }
    }
    $status = Invoke-LabCommand 'docker compose exec -T ui curl -fsS --max-time 10 http://catalog:8080/chaos/status'
    $chaos = $status | ConvertFrom-Json
    if ($chaos.latency.enabled -or $chaos.error_status.enabled) { throw 'catalog 已有故障开关开启，先恢复原实验。' }
    $null = Invoke-LabCommand 'docker compose exec -T ui curl -fsS --max-time 10 http://catalog:8080/health'
    $products = Invoke-LabCommand 'docker compose exec -T ui curl -fsS --max-time 10 http://catalog:8080/catalog/products'
    if ($products -notmatch '"id"') { throw '商品接口缺少商品内容，不能作为健康基线。' }
    return '13 个容器运行且已有健康检查通过；catalog 开关关闭，健康和商品接口正常。此检查不替代完整下单验证。'
}

if ($Action -eq 'Plan') {
    [pscustomobject]@{
        CaseId = $case.id
        Title = $case.title
        PromptForDotOps = $case.prompt
        InjectCommand = $case.commands.inject
        RecoverCommand = $case.commands.recover
        Status = '仅展示，未连接主机、未注入故障'
    } | Format-List
    return
}

foreach ($path in @($IdentityFile, $KnownHostsFile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少 SSH 文件：$path" }
}
if (($Action -eq 'Inject' -or $Action -eq 'Recover') -and -not $case.commands) {
    throw "$CaseId 是健康基线，用 Inspect 检查，不执行注入或恢复。"
}

# 每次动作单独留痕。故障真相只给实验者，给 Agent 的输入仅用 prompt 字段。
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$actionId = '{0}-{1}-{2}-{3}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $CaseId, $Action, ([guid]::NewGuid().ToString('N').Substring(0, 8))
$recordPath = Join-Path $OutputDirectory "$actionId.json"
$record = [ordered]@{
    action_id = $actionId
    case_id = $CaseId
    action = $Action
    started_at = [DateTimeOffset]::Now.ToString('o')
    host = $LabHost
    status = 'started'
    fault_truth = $case.truth
    recovery_command = $case.commands.recover
    prompt_for_dotops = $case.prompt
    command_output = $null
    baseline_check = $null
    business_recovery_verified = $false
    error = $null
}
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding UTF8
try {
    switch ($Action) {
        'Inspect' {
            # 远端参数使用单引号，避免 Windows PowerShell 启动 SSH 时丢失内层双引号。
            $record.command_output = Invoke-LabCommand 'docker compose ps --all --format ''{{.Service}} {{.State}} {{.Health}}'' && docker compose exec -T ui curl -sS --max-time 10 -w '' HTTP%{http_code}'' http://catalog:8080/chaos/status http://catalog:8080/health http://catalog:8080/catalog/products'
        }
        'Inject' {
            $record.baseline_check = Assert-LabBaseline
            $record.command_started_at = [DateTimeOffset]::Now.ToString('o')
            $record.command_output = Invoke-LabCommand $case.commands.inject
        }
        'Recover' {
            $record.command_output = Invoke-LabCommand $case.commands.recover
        }
    }
    $record.status = 'command_completed'
    $record.command_completed_at = [DateTimeOffset]::Now.ToString('o')
    Write-Host $record.command_output
    if ($Action -eq 'Inject') {
        Write-Host "故障命令已完成，请立即触发对应业务并记录现象；恢复：.\Invoke-RetailFault.ps1 -CaseId $CaseId -Action Recover"
    }
    if ($Action -eq 'Recover') { Write-Host '恢复命令已完成；请等待健康检查，重新验证商品页面和内容，再填写业务恢复结果。' }
} catch {
    $record.status = 'failed_or_outcome_unknown'
    $record.error = $_.Exception.Message
    throw
} finally {
    $record.finished_at = [DateTimeOffset]::Now.ToString('o')
    $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding UTF8
    Write-Host "动作记录：$recordPath"
}
