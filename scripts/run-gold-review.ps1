<#
.SYNOPSIS
  Runs the weekly Claude Strategy Review (the gold-review skill) headless, with a restricted tool set.

.DESCRIPTION
  Used by the "FXCommand GOLD review" scheduled task (install-review-task.ps1), or run it by hand for an
  on-demand review. It needs the FXCommand app running on http://127.0.0.1:8000 (the review reads it
  through the read-only MCP tools and the research CLI) and the Claude Code CLI ("claude") logged in.

  Tool boundary (spec D54): the review runs with --permission-mode dontAsk, so every tool not listed in
  $Allowed is refused without asking. The operator MCP tools (start/pause/resume/stop Session, Kill
  Switch, run learning) are not allowed and are also listed in $Denied; Bash is limited to the research
  CLI, pytest and the git/gh steps of opening a review PR. Only the fxcommand MCP server is loaded
  (--strict-mcp-config): none of your other MCP connectors.

  Limits of this boundary: the HTTP API on 127.0.0.1 has no authentication, and code the reviewer writes
  runs under pytest. The allow-list stops the reviewer from calling operator actions directly; it is not
  a sandbox for code it writes. Review its PRs before merging.

  The review works in its own git worktree (..\fxcommand-review, reset to origin/<BaseBranch> each run),
  never in your checkout. Output: backend\data\logs\gold-review-YYYYMMDD.log.
#>
param(
    [string]$BaseBranch = "main",
    [double]$MaxBudgetUsd = 15,
    [string]$Model = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$worktree = Join-Path (Split-Path -Parent $root) "fxcommand-review"
$logs = Join-Path $root "backend\data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs ("gold-review-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Say($msg) { Add-Content $log ("==== {0} {1}" -f (Get-Date -Format "s"), $msg) }

$Allowed = @(
    "Read", "Grep", "Glob", "Write", "Edit", "TodoWrite",
    "mcp__fxcommand__get_overview", "mcp__fxcommand__list_sessions", "mcp__fxcommand__list_positions",
    "mcp__fxcommand__get_journal", "mcp__fxcommand__get_preflight", "mcp__fxcommand__get_learning_status",
    "mcp__fxcommand__get_evidence", "mcp__fxcommand__get_scorecard", "mcp__fxcommand__get_research_snapshot",
    "mcp__fxcommand__get_trials", "mcp__fxcommand__submit_review", "mcp__fxcommand__submit_challenger",
    "Bash(uv run --directory backend fxcommand-research:*)",
    "Bash(uv run --directory backend pytest:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git switch -c review/:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git push -u origin review/:*)", "Bash(gh pr create:*)"
)
$Denied = @(
    "mcp__fxcommand__start_session", "mcp__fxcommand__pause_session", "mcp__fxcommand__resume_session",
    "mcp__fxcommand__stop_session", "mcp__fxcommand__kill_switch", "mcp__fxcommand__run_learning",
    "Bash(gh pr merge:*)", "Bash(git push origin main:*)", "Bash(curl:*)", "WebFetch", "WebSearch",
    "Edit(.claude/**)", "Write(.claude/**)", "Edit(.mcp.json)", "Write(.mcp.json)"  # it may not widen its own settings
)

try {
    Say "GOLD review starting (base origin/$BaseBranch)"
    try { Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/status" -TimeoutSec 10 | Out-Null }
    catch { Say "FXCommand is not running on :8000 - review skipped"; exit 1 }

    git -C $root fetch origin $BaseBranch *>> $log
    if (-not (Test-Path $worktree)) {
        git -C $root worktree add --detach $worktree "origin/$BaseBranch" *>> $log
    } else {
        git -C $worktree switch --detach --discard-changes "origin/$BaseBranch" *>> $log
        git -C $worktree clean -fd *>> $log
    }
    Push-Location (Join-Path $worktree "backend")
    try { uv sync --quiet *>> $log } finally { Pop-Location }

    $claudeArgs = @("-p", "/gold-review", "--permission-mode", "dontAsk",
              "--mcp-config", (Join-Path $worktree ".mcp.json"), "--strict-mcp-config",
              "--max-budget-usd", "$MaxBudgetUsd",
              "--allowedTools") + $Allowed + @("--disallowedTools") + $Denied
    if ($Model) { $claudeArgs += @("--model", $Model) }
    Push-Location $worktree
    try {
        & claude @claudeArgs *>> $log
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    Say "GOLD review finished with code $code"
    exit $code
} catch {
    Say "GOLD review failed: $_"
    exit 1
}
