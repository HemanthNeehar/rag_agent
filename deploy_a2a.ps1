# Deploy RAG agent to Vertex Agent Engine as A2A (A2aAgent + Executor).
# Prefer this path over a generic "adk deploy" if you need A2A + Agent registry class_method wiring.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$repoRoot = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Host "[deploy_a2a] Using $venvPython" -ForegroundColor DarkGray
    Set-Location $repoRoot
    & $venvPython -m rag_agent.deploy @args
}
else {
    Write-Warning "[deploy_a2a] $repoRoot\.venv not found - using 'python' on PATH."
    Set-Location $repoRoot
    python -m rag_agent.deploy @args
}
