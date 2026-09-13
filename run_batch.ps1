# Full run on the Batches API (half price). Usage:  powershell -File run_batch.ps1
$ErrorActionPreference = "Stop"

if (-not $env:MW_PORTKEY_PROVIDER) {
  Write-Host "MW_PORTKEY_PROVIDER is not set. Batches need it (with the @):" -ForegroundColor Yellow
  Write-Host '  $env:MW_PORTKEY_PROVIDER = "@your-provider-slug"'
  exit 1
}

Write-Host "`n== 1. read the boards ==" -ForegroundColor Cyan
python batch_pipeline.py submit  extract
python batch_pipeline.py collect extract --wait

Write-Host "`n== 2. solve and verify ==" -ForegroundColor Cyan
python batch_pipeline.py submit  solve
python batch_pipeline.py collect solve --wait
python batch_pipeline.py resolve-refuted     # batches cannot escalate mid-flight

Write-Host "`n== 3. build the questions ==" -ForegroundColor Cyan
python batch_pipeline.py submit  mcq
python batch_pipeline.py collect mcq --wait

Write-Host "`n== 4. check and build ==" -ForegroundColor Cyan
python pipeline.py qa
python pipeline.py build
python pipeline.py index
python pipeline.py estimate
