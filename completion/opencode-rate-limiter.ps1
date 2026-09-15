# opencode-rate-limiter PowerShell completion
# Install: add this file to your $PROFILE, e.g.
#   opencode-rate-limiter completions powershell >> $PROFILE
Register-ArgumentCompleter -Native -CommandName @('opencode-rate-limiter') -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @(
        "check"
        "completions"
        "daemon"
        "deep"
        "diagnose"
        "explain"
        "generate-config"
        "generate-launchd"
        "generate-systemd"
        "generate-task"
        "headers"
        "probe"
        "quick"
        "rotate"
    )
    $probeModels = @(
        "deepseek-v4-flash-free"
        "nemotron-3-ultra-free"
        "big-pickle"
        "mimo-v2.5-free"
        "hy3-free"
        "laguna-s-2.1-free"
        "ling-3.0-flash-fin-free"
        "nemotron-3.5-lightning-free"
        "all"
    )
    $strategies = @( "round_robin" "least_used" "health")
    $elements = $commandAst.ToString() -split '\s+'
    $sub = $elements | Where-Object { $_ -in $commands } | Select-Object -First 1
    if (-not $sub) {
        $commands | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
        return
    }
    $candidates = switch ($sub) {
        "check" { @("--trend" "--export-events" "--export-format") }
        "daemon" { @("--interval" "--models" "--once" "--stop") }
        "diagnose" { @("--model" "--from-text" "--from-log") }
        "explain" { @("--from-log") }
        "generate-config" { @("--force") }
        "headers" { @("--model" "--export") }
        "rotate" { @("--strategy" "--to" "--apply") }
        default { @() }
    }
    if ($sub -eq 'probe') { $candidates += $probeModels }
    if ($elements -contains '--strategy') { $candidates += $strategies }
    $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
