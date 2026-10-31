# Pre-ship verification gate.
#
# Runs the full validation pipeline locally before a change is declared
# "done": byte-compile, the non-e2e pytest suite, the static ratchet (no new
# duplicate block / unused import / likely bug or security finding beyond the
# committed baseline), the docs
# ratchet (no new history marker, README within its size budget), the structure
# ratchet (no file/function/directory growth, root layout matches the shape), the
# conventions ratchet (no fleet-convention rule count above the baseline), the
# comment ratchet (no more issue references in code comments than the baseline),
# then the Playwright e2e suite (Chromium — this app is Android-only) against a disposable
# webapp the script boots itself on a free port, then the screenshot baseline
# (no visible change against the committed PNGs under tests/e2e/shots).
#
# Usage:
#   pwsh -File scripts/verify-before-ship.ps1
#   powershell -File scripts\verify-before-ship.ps1   # Windows PowerShell 5.1 works too
#
# A tray on :8465 may be running or not — autoboot picks a free port for its
# own webapp and adopts the tray's session-host on :8466 if one is up. Exits
# non-zero on the first failure with the offending output left visible.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# Persistent progress log: phase markers land here from this
# script, per-test START/DONE lines from the pytest hook in tests/conftest.py
# (via LAUNCHER_VERIFY_PROGRESS_LOG). If an outer timeout kills the gate, the
# last lines name the active phase + node id and the per-test timings survive.
# Gitignored via the blanket *.log rule; overwritten each run.
$progressLog = Join-Path $repoRoot "webapp\verify-progress.log"
[void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $progressLog))
Set-Content -Path $progressLog -Encoding UTF8 -Value (
    "verify-before-ship run started {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
)
$env:LAUNCHER_VERIFY_PROGRESS_LOG = $progressLog

function Log-Progress($message) {
    Add-Content -Path $progressLog -Encoding UTF8 -Value (
        "[{0} +{1,7:n1}s] {2}" -f (Get-Date -Format "HH:mm:ss"), $sw.Elapsed.TotalSeconds, $message
    )
}

function Phase($message) {
    Write-Host "==> $message" -ForegroundColor Cyan
    Log-Progress "==> phase: $message"
}

function Fail($message) {
    Write-Host ""
    Write-Host "[X] $message" -ForegroundColor Red
    Write-Host ("Failed after {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Red
    Log-Progress ("FAILED: {0} (after {1:n1}s)" -f $message, $sw.Elapsed.TotalSeconds)
    Remove-Item Env:\LAUNCHER_VERIFY_PROGRESS_LOG -ErrorAction SilentlyContinue
    exit 1
}

if (-not (Test-Path $python)) {
    Fail ".venv missing -- run setup.bat first."
}

Push-Location $repoRoot

. (Join-Path $PSScriptRoot "box_lock_gate.ps1"); Enter-BoxLockGate
try {
    Phase "py_compile (app, src, tests)..."
    & $python -m compileall -q app src tests
    if ($LASTEXITCODE -ne 0) { Fail "byte-compile failed." }

    Phase "pytest (non-e2e)..."
    & $python -m pytest -q --ignore=tests/e2e
    if ($LASTEXITCODE -ne 0) { Fail "non-e2e pytest suite failed." }

    # Static ratchet (scaffold docs/static-ratchet.md): no new duplicate block
    # (jscpd via npx), unused import/variable (ruff F401,F841) or likely bug /
    # security finding (curated ruff B and S rules, S113 strict) beyond
    # .static-ratchet-baseline.json. Whole tree, so never diff-routed. Runs
    # through the wrapper, which fails exit 3 (a detector could not run) as
    # NOT CONFIRMED instead of letting it read as a pass.
    Phase "static ratchet..."
    & $python -m scripts.static_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "static ratchet failed or NOT CONFIRMED: a new duplicate block or unused import/variable, a detector that could not run (Node/npx or ruff missing), or a baseline problem; see the output above and .static-ratchet-baseline.json." }

    # Docs ratchet (scaffold docs/docs-ratchet.md): README.md within its size
    # budget and no new history marker (issue reference, dated or changelog
    # heading) in README.md / docs/**/*.md beyond .docs-ratchet-baseline.json.
    # Whole tree, so never diff-routed. Same wrapper shape as the static ratchet:
    # exit 3 (a file could not be read) is NOT CONFIRMED and fails.
    Phase "docs ratchet..."
    & $python -m scripts.docs_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "docs ratchet failed or NOT CONFIRMED: a new history marker, a README past its size budget, a file that could not be read, or a baseline problem; see the output above and .docs-ratchet-baseline.json." }

    # Structure ratchet (scaffold docs/structure-ratchet.md): no file past 1,000
    # lines, function past 100, directory past 40 modules or src->app import
    # beyond .structure-ratchet-baseline.json, and root folders matching the
    # shape declared in .fleet.toml (docs/layout.md). Whole tree, so never
    # diff-routed. Same wrapper shape as the other ratchets: exit 3 (part of the
    # tree could not be measured) is NOT CONFIRMED and fails.
    Phase "structure ratchet..."
    & $python -m scripts.structure_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "structure ratchet failed or NOT CONFIRMED: a file, function or directory grew past its baseline, a new offender, a root folder outside the declared shape, a file that could not be measured, or a baseline problem; see the output above, docs/layout.md and .structure-ratchet-baseline.json." }

    # Instruction ratchet (scaffold docs/instruction-ratchet.md): no instruction file
    # (CLAUDE.md, AGENTS.md, .claude/rules, SKILL.md and the docs a skill links to) may
    # carry more weekly-prompt-audit rule hits than .instruction-ratchet-baseline.json
    # records or pass its size cap. Whole tree, so never diff-routed. Same wrapper shape
    # as the other ratchets: exit 3 (a file could not be read) is NOT CONFIRMED and fails.
    Phase "instruction ratchet..."
    & $python -m scripts.instruction_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "instruction ratchet failed or NOT CONFIRMED: an instruction file has a new rule hit or passed its size cap, a file could not be read, or a baseline problem; see the output above and .instruction-ratchet-baseline.json." }

    # Doc-refs ratchet (scaffold docs/doc-refs-ratchet.md): README.md, CLAUDE.md and
    # docs/**/*.md may not name a repo path, python -m module, relative link or loopback
    # port that does not exist beyond .doc-refs-ratchet-baseline.json. Whole tree, so
    # never diff-routed: a code change can strand a doc reference without touching the
    # doc. Same wrapper shape as the other ratchets: exit 3 (git or a doc could not be
    # read) is NOT CONFIRMED and fails.
    Phase "doc refs ratchet..."
    & $python -m scripts.doc_refs_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "doc refs ratchet failed or NOT CONFIRMED: a doc names a path, module, link or port that does not exist, git or a doc could not be read, or a baseline problem; see the output above and .doc-refs-ratchet-baseline.json." }

    # Dead-code ratchet (scaffold docs/dead-code-ratchet.md): no new function, class or
    # method that nothing in the tracked Python tree refers to, beyond
    # .dead-code-ratchet-baseline.json. Whole tree, so never diff-routed: deleting the
    # last caller in one file strands a definition in another. Same wrapper shape as the
    # other ratchets: exit 3 (git unusable, or a .py file that cannot be read or parsed)
    # is NOT CONFIRMED and fails.
    Phase "dead code ratchet..."
    & $python -m scripts.dead_code_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "dead code ratchet failed or NOT CONFIRMED: a function, class or method nothing references, git or a Python file could not be read or parsed, or a baseline problem; see the output above and .dead-code-ratchet-baseline.json." }

    # Conventions ratchet (scaffold docs/conventions-ratchet.md): fleet conventions held
    # as ast rules (no-window, logging-not-print, no-drive-path, secret-compare,
    # git-wrapper); no rule's per-file count may rise above
    # .conventions-ratchet-baseline.json. Whole tree, so never diff-routed: a rule added
    # to `rules` must see every file. Same wrapper shape as the other ratchets: exit 3
    # (git unusable, or a .py file that cannot be read or parsed) is NOT CONFIRMED and fails.
    Phase "conventions ratchet..."
    & $python -m scripts.conventions_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "conventions ratchet failed or NOT CONFIRMED: a convention rule's count rose above its baseline, git or a Python file could not be read or parsed, or a baseline problem; see the output above and .conventions-ratchet-baseline.json." }

    # Comment ratchet (scaffold docs/comment-ratchet.md): no file may carry more
    # issue references (#NNN) inside code comments than .comment-ratchet-baseline.json
    # records, and a new file may carry none. Whole tree, so never diff-routed. Same
    # wrapper shape as the other ratchets: exit 3 (a file could not be scanned) is
    # NOT CONFIRMED and fails.
    Phase "comment ratchet..."
    & $python -m scripts.comment_ratchet_gate
    if ($LASTEXITCODE -ne 0) { Fail "comment ratchet failed or NOT CONFIRMED: a file carries more issue references in comments than its baseline, a file could not be scanned, or a baseline problem; see the output above and .comment-ratchet-baseline.json." }

    # Diff-proportionate e2e routing. Classify the branch's
    # changed files vs main and run an e2e slice proportionate to the diff:
    # static assets -> Chromium smoke only, real UI/behaviour -> the full
    # suite, backend/docs-only -> no browser suite. Fail-safe: any
    # mixed/ambiguous/unrecognized diff runs the full suite. The path->tier
    # rules live in scripts/classify_e2e.py (one reviewable place). On CI the
    # full suite always runs -- the local gate is where routing is proven first.
    $tier = "full"; $e2eTarget = "tests/e2e"; $e2eBrowsers = ""; $routeReason = ""
    $sessionHostTouched = "unknown"
    if ($env:CI -eq "true") {
        $routeReason = "CI always runs the full e2e suite"
    } else {
        $classifyOut = & $python (Join-Path $repoRoot "scripts\classify_e2e.py")
        $kv = @{}
        foreach ($line in $classifyOut) {
            if ($line -match '^(E2E_[A-Z_]+)=(.*)$') { $kv[$matches[1]] = $matches[2] }
        }
        if ($kv.ContainsKey("E2E_TIER") -and $kv["E2E_TIER"]) {
            $tier = $kv["E2E_TIER"]
            $e2eTarget = $kv["E2E_PYTEST_TARGET"]
            $e2eBrowsers = $kv["E2E_BROWSERS"]
            $routeReason = $kv["E2E_REASON"]
            if ($kv.ContainsKey("E2E_SESSION_HOST_TOUCHED")) { $sessionHostTouched = $kv["E2E_SESSION_HOST_TOUCHED"] }
        } else {
            $routeReason = "classifier gave no verdict -- defaulting to full suite (fail-safe)"
        }
    }

    if ($tier -eq "skip") {
        Phase "e2e routing: SKIP browser suite (backend/docs-only diff)"
        Write-Host "    reason: $routeReason" -ForegroundColor DarkGray
        Log-Progress "e2e routing: tier=skip reason=$routeReason"
        Write-Host "    shot baseline skipped too: no screen was recaptured or compared" -ForegroundColor DarkGray
    } else {
        Phase "e2e routing: $tier ($routeReason)"
        Log-Progress "e2e routing: tier=$tier target=$e2eTarget browsers=$e2eBrowsers reason=$routeReason"

        # Serialize full-tier e2e legs across concurrent primary/worktree gate
        # runs on this machine. Two overlapping full-tier runs --
        # a normal outcome of the fleet's own claim-or-worktree concurrency
        # model -- each spinning up ~400 browser contexts is the
        # ephemeral-port burst this suite can cause (Tcpip
        # 4231, TIME_WAIT 206->979). A kernel-managed
        # named mutex, not a lockfile: auto-released if a holder crashes, no
        # stale-file cleanup path. Scoped to only this e2e leg -- byte-compile
        # and the non-e2e pytest phase above never wait on it -- and only to
        # the "full" tier; "static" (chromium-smoke-only) runs are cheap
        # enough not to need it.
        $e2eMutexName = "Global\AppLauncherFullE2EGate"
        $e2eMutex = $null
        $e2eMutexAcquired = $false
        if ($tier -eq "full") {
            $e2eMutex = New-Object System.Threading.Mutex($false, $e2eMutexName)
            $e2eMutexAcquired = $e2eMutex.WaitOne(0)
            if (-not $e2eMutexAcquired) {
                Phase "e2e gate: queued behind another full gate run (waiting on $e2eMutexName)..."
                Log-Progress "e2e gate: queued -- another verify-before-ship full e2e run holds $e2eMutexName"
                $e2eMutexAcquired = $e2eMutex.WaitOne([TimeSpan]::FromMinutes(30))
                if (-not $e2eMutexAcquired) {
                    $e2eMutex.Dispose()
                    Fail ("Timed out after 30 min waiting for another full e2e gate run to release " +
                          "$e2eMutexName -- check for a stuck/hung gate elsewhere on this machine (issue #685).")
                }
                Log-Progress "e2e gate: acquired $e2eMutexName after queueing"
            }
        }

        try {
            $env:LAUNCHER_E2E_AUTOBOOT = "1"
            # Taken before the run: a shot written after it was recaptured by it.
            $shotSince = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            # On CI run verbose + unbuffered so a hung test (pytest-timeout aborts
            # the process via os._exit, skipping the summary) is named by the last
            # nodeid logged at test start. Locally keep the compact dotted output.
            $verbosity = if ($env:CI -eq "true") { "-v" } else { "-q" }
            if ($env:CI -eq "true") { $env:PYTHONUNBUFFERED = "1" }
            $e2eArgs = @($e2eTarget, $verbosity)
            foreach ($b in ($e2eBrowsers -split ',' | Where-Object { $_ })) {
                $e2eArgs += @("--browser", $b)
            }
            $projLabel = if ($e2eBrowsers) { $e2eBrowsers } else { "Chromium" }
            Phase "pytest e2e ($e2eTarget, $projLabel, auto-booted)..."
            try {
                & $python -m pytest @e2eArgs
                $e2eExit = $LASTEXITCODE
            }
            finally {
                Remove-Item Env:\LAUNCHER_E2E_AUTOBOOT -ErrorAction SilentlyContinue
                if ($env:CI -eq "true") { Remove-Item Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue }
            }
            if ($e2eExit -ne 0) { Fail "Playwright e2e suite failed." }

            # Screenshot baseline (scaffold docs/playwright-ui-testing.md): the last
            # stage. A slice that ran the whole suite or the capture test just
            # recaptured the shots, so only compare; a narrower slice did not, so
            # the stage captures them itself (it needs the autoboot switch the e2e
            # leg above already cleared). The baseline belongs to the machine that
            # captured it -- fonts and the rasteriser differ on a CI runner -- so
            # CI leaves the comparison to the local gate.
            if ($env:CI -eq "true") {
                Phase "shot baseline: skipped on CI (the baseline is this machine's)"
            } else {
                $env:LAUNCHER_E2E_AUTOBOOT = "1"
                # The vendored script spawns git without setting this itself.
                $env:GIT_OPTIONAL_LOCKS = "0"
                try {
                    if (@("tests/e2e", "tests/e2e/screens/test_screen_shots.py") -contains $e2eTarget) {
                        $since = $shotSince.ToString([Globalization.CultureInfo]::InvariantCulture)
                        Phase "shot baseline (compare)..."
                        & $python scripts/shot_baseline.py --check-tree --since $since
                    } else {
                        Phase "shot baseline (capture + compare)..."
                        & $python scripts/shot_baseline.py --baseline
                    }
                    $shotExit = $LASTEXITCODE
                }
                finally {
                    Remove-Item Env:\LAUNCHER_E2E_AUTOBOOT -ErrorAction SilentlyContinue
                    Remove-Item Env:\GIT_OPTIONAL_LOCKS -ErrorAction SilentlyContinue
                }
                if ($shotExit -eq 3) { Fail "shot baseline NOT CONFIRMED (no baseline, no shot recaptured, an unreadable image, or the capture failed)." }
                if ($shotExit -ne 0) { Fail "shot baseline failed (exit $shotExit): a screen drifted from tests/e2e/shots. A deliberate UI change re-baselines (CLAUDE.md); anything else is unpinned content." }
            }
        }
        finally {
            if ($e2eMutex) {
                if ($e2eMutexAcquired) { $e2eMutex.ReleaseMutex() }
                $e2eMutex.Dispose()
            }
        }
    }
}
finally {
    Pop-Location
}

$sw.Stop()
Log-Progress ("OK: all checks passed in {0:n1}s" -f $sw.Elapsed.TotalSeconds)
Remove-Item Env:\LAUNCHER_VERIFY_PROGRESS_LOG -ErrorAction SilentlyContinue
Write-Host ""
Write-Host ("[OK] Ready to ship -- all checks passed in {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green

# A green gate + merge + tray.bat --restart still does not make a
# session-host-path change live -- that process is deliberately excluded
# from the reclaim sweep and can run stale code for
# days with nothing else surfacing that. The classifier answers from CLAUDE.md's
# `## session-host` block (the list /api/version's stale_relevant also reads), not
# from its e2e route labels, so the warning cannot drift from that list. On CI the
# classifier is not run, so the answer stays "unknown" and nothing is printed.
if ($sessionHostTouched -eq "yes") {
    Write-Host ""
    Write-Host "[!] SESSION-HOST PATHS TOUCHED -- this is NOT live after tray.bat --restart." -ForegroundColor Yellow
    Write-Host "    Check GET /api/version's session_host.stale_relevant field before reporting" -ForegroundColor Yellow
    Write-Host "    this change as shipped -- if true, report it as merged but not yet live." -ForegroundColor Yellow
    Write-Host "    (Raw session_host.stale is true after ANY merge, not just a session-host" -ForegroundColor Yellow
    Write-Host "    one -- #635; null in either field means unknown, never a confident false.)" -ForegroundColor Yellow
    Write-Host "    See CLAUDE.md's restart section for the one supported way to restart :8466." -ForegroundColor Yellow
}
exit 0
