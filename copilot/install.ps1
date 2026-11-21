# install.ps1 - wire this checkout's Copilot side into GitHub Copilot CLI (user scope).
#
#   .\copilot\install.ps1             install (idempotent; re-run after moving the checkout)
#   .\copilot\install.ps1 -Uninstall  remove exactly what this installer put in ~/.copilot
#   .\copilot\install.ps1 -Migrate    move a machine off the old fleet-config-lite install, then install
#   add -WhatIf to any of them        list every change it would make, and make none
#
# Step-by-step guide for each case (fresh machine, -Migrate, a machine with its
# own skills repo, uninstall): copilot\docs\install.md.
#
# What install writes, and where:
# 1. Hook configs: renders hooks\session-state.template.json and
#    hooks\chief-guard.template.json with this checkout's absolute path and a
#    resolved Python executable, into %USERPROFILE%\.copilot\hooks\
#    app-launcher-lite-<name>.json, and makes sure hooks\state exists.
# 2. Skills: junctions skills\<name> into %USERPROFILE%\.copilot\skills\<name>,
#    only into a real folder and never over a skill this checkout does not own.
#    When Lite's skills_dir setting (config\webapp_config.json) names a folder,
#    the four issue skills (issue-add, issue-start, issue-yolo, issue-finish)
#    come from it instead: each is junctioned from that folder into
#    ~/.copilot/skills, and into ~/.claude/skills when that is a real folder, and
#    Lite's bundled copies of those four are not installed (earlier ones of its
#    own are unlinked). Links it makes into skills_dir are listed in
#    ~/.copilot/app-launcher-lite-install.json, which is how -Uninstall knows them.
#    An existing entry that is not Lite's is kept and reported, and counts as
#    that skill.
# 3. Config: adds skills_dir (empty) to config\webapp_config.json only when the
#    key is missing, after a timestamped backup. No existing value is changed; a
#    file that does not parse is reported and left alone. config\config.json is a
#    committed file and is never edited.
# 4. A fresh-machine checklist (Copilot CLI, glab and its sign-in for the GitLab
#    host, whether git reaches GitLab with no prompt, the venv, the session-host
#    on the port config\config.json sets), unless -SkipChecklist.
#
# What is Lite's own, so what -Uninstall may remove:
# - a hook config named app-launcher-lite-*.json whose commands run this
#   checkout's copilot\hooks scripts;
# - a skill junction whose target is under this checkout's copilot\skills (or a
#   dangling one ending in copilot\skills\<name>, left by a moved checkout), and
#   a skill copy that carries the .app-launcher-lite-managed marker file;
# - a skills_dir link recorded in app-launcher-lite-install.json that still
#   points where it was recorded.
# A re-run reports each item it already has as unchanged and rewrites nothing.
# Anything else in ~/.copilot is the owner's or another tool's and is never
# touched. hooks\state (the session-state file, the chief's plan, journal and
# mailbox) is never read, moved or deleted by any mode.
#
# -Migrate removes the old fleet-config-lite install first (its two hook configs
# and its skill junctions), because two session-state hooks would race on the
# same state file. It lists what it would remove and asks; -Yes answers for it.
# It also checks Lite's Copilot folder setting (fleet_config_dir): an explicit
# path to the old fleet-config-lite folder keeps the chief there, so it offers
# to point the setting at this checkout's copilot folder through Lite's own
# Settings API (the one field, nothing else). Stop the chief at a turn end
# first; the order is in docs\migrating-from-fleet-config-lite.md.
#
# Shared machine: when ~/.copilot/skills or ~/.agents/skills is a link into
# another tree, this PC also runs the full fleet. Then no skill is written, and
# the session-state hook is rendered with --lite-sessions-only so it records
# only sessions app-launcher-lite launched. The chief-guard hooks are installed
# as usual; they act only for lite's chief and its lanes and return at once for
# every other session. Any user-scope target that is itself a link into another
# tree (the Copilot home, its hooks folder, a hook config, the skills folder) is
# never written through: it is skipped with a message.
#
# This installer never edits ~/.claude/settings.json, any agent's trust
# settings, or git config.
#
# NOTE for agents and future edits: ASCII only in this file - it can run
# under Windows PowerShell 5.1, which chokes on BOM-less non-ASCII.

param(
    [switch]$Uninstall,
    [switch]$Migrate,
    [switch]$Yes,
    [switch]$SkipChecklist,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

if ($Uninstall -and $Migrate) {
    Write-Error '-Uninstall and -Migrate cannot be combined: migrate installs, uninstall removes.'
}

$copilotRepo = $PSScriptRoot
$repo = Split-Path -Parent $copilotRepo
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $env:USERPROFILE '.copilot' }
$hooksDir = Join-Path $copilotHome 'hooks'
$stateDir = Join-Path $hooksDir 'state'
$skillsDir = Join-Path $copilotHome 'skills'
$agentsSkillsDir = Join-Path (Join-Path $env:USERPROFILE '.agents') 'skills'
$ourSkills = Join-Path $copilotRepo 'skills'
$skillMarker = '.app-launcher-lite-managed'
$claudeSkillsDir = Join-Path (Join-Path $env:USERPROFILE '.claude') 'skills'
$manifestPath = Join-Path $copilotHome 'app-launcher-lite-install.json'
$webappConfigPath = Join-Path $repo 'config\webapp_config.json'
$issueSkills = @('issue-add', 'issue-start', 'issue-yolo', 'issue-finish')

# Hyphens only in the filename: Copilot CLI 1.0.70 silently ignores hook files
# with more than one dot in the name (verified live).
$hookConfigs = @(
    @{ Template = 'session-state.template.json'; File = 'app-launcher-lite-session-state.json'; Script = 'session_state.py' },
    @{ Template = 'chief-guard.template.json'; File = 'app-launcher-lite-chief-guard.json'; Script = 'chief_guard.py' }
)
# What the old two-repo install wrote, and the folder name its skill junctions point into.
$oldHookFiles = @('fleet-config-lite-session-state.json', 'fleet-config-lite-chief-guard.json')
$oldSkillTarget = '[\\/]fleet-config-lite[^\\/]*[\\/]skills[\\/]'

# The link target when $path is a reparse point, else $null.
function Get-LinkTarget([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not $item -or -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
    $target = [string]($item.Target | Select-Object -First 1)
    if (-not $target) { return '(unresolved link)' }
    return $target
}

# The link target when $path is a reparse point that does not point into this
# checkout, else $null: writing through it would land in another tree.
function Get-ForeignLink([string]$path) {
    $target = Get-LinkTarget $path
    if (-not $target) { return $null }
    # The separator keeps a sibling checkout (app-launcher-lite-wt-7) from passing as this one.
    if ($target.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $target
}

# True when a hook config at $path is one this checkout's installer wrote: its
# commands run this repo layout's copilot\hooks\<script>.
function Test-OurHook([string]$path, [string]$script) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    return ([IO.File]::ReadAllText($path)).Contains("copilot/hooks/$script")
}

# True when a link or folder in the skills dir is one this installer made.
function Test-OurSkill($item) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $target = [string]($item.Target | Select-Object -First 1)
        if (-not $target) { return $false }
        if ($target.StartsWith($ourSkills + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
        # A moved checkout leaves a dangling link whose target still ends in our layout.
        return (-not (Test-Path -LiteralPath $target)) -and ($target -match '[\\/]copilot[\\/]skills[\\/][^\\/]+$')
    }
    return Test-Path -LiteralPath (Join-Path $item.FullName $skillMarker) -PathType Leaf
}

# Delete a link only (Remove-Item -Recurse on a junction can walk into its
# target); a marked copy is a real folder and goes with its contents.
function Remove-SkillEntry($item) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $item.Delete() } else { Remove-Item -LiteralPath $item.FullName -Recurse -Force }
}

# One question, one answer. -Yes answers it. [Console]::ReadLine, not Read-Host:
# the AllSigned-safe launcher starts PowerShell with -NonInteractive, where
# Read-Host throws. No line to read (closed stdin) is a refusal with the reason,
# never a silent yes.
function Confirm-Step([string]$question) {
    if ($WhatIf) {
        Write-Host "[whatif] would ask: $question"
        return $true
    }
    if ($Yes) { return $true }
    Write-Host -NoNewline "$question [y/N] "
    $answer = [Console]::ReadLine()
    if ($null -eq $answer) {
        Write-Host ''
        Write-Host '[skip] no answer to read (no terminal); re-run with -Yes to accept'
        return $false
    }
    return ($answer -match '^\s*y(es)?\s*$')
}

# Every change any mode makes goes through here, so -WhatIf lists each one and
# makes none. $what reads as the outcome ("hook config removed -> <path>").
function Invoke-Change([string]$what, [scriptblock]$action) {
    if ($WhatIf) {
        Write-Host "[whatif] $what"
        return
    }
    $null = & $action
    Write-Host "[ok] $what"
}

function Test-SamePath([string]$a, [string]$b) {
    try {
        return [IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [IO.Path]::GetFullPath($b).TrimEnd('\')
    } catch { return $false }
}

function Test-Link($item) {
    return [bool]($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

# --- the install record: the skills_dir links this installer made. A junction
# --- into another repo cannot carry a marker file, so ownership is this list.
function Read-InstallRecord {
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return @() }
    try {
        $body = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    } catch {
        Write-Host "[unknown] install record did not parse, so no skills_dir link is treated as Lite's -> $manifestPath"
        return @()
    }
    return @($body.skill_links | Where-Object { $_.path -and $_.target } | ForEach-Object { @{ path = [string]$_.path; target = [string]$_.target } })
}

function Save-InstallRecord($links, [bool]$changed) {
    if (-not $changed) { return }
    if (@($links).Count -eq 0) {
        if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
            Invoke-Change "install record removed (no skills_dir link left) -> $manifestPath" { Remove-Item -LiteralPath $manifestPath -Force }
        }
        return
    }
    $json = ConvertTo-Json -InputObject @{ skill_links = @($links) } -Depth 4
    Invoke-Change "install record written -> $manifestPath" { [IO.File]::WriteAllText($manifestPath, $json) }
}

# The record entry for a link at $path that still points where it was recorded, else $null.
function Find-RecordedLink($links, [string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not (Test-Link $item)) { return $null }
    foreach ($link in $links) {
        if ((Test-SamePath $link.path $path) -and (Test-SamePath $link.target ([string]($item.Target | Select-Object -First 1)))) { return $link }
    }
    return $null
}

# --- Lite's skills_dir setting: state empty | set | unknown, with the folder when set.
function Get-SkillsDirSetting {
    if (-not (Test-Path -LiteralPath $webappConfigPath -PathType Leaf)) { return @{ State = 'empty' } }
    try {
        $value = [string](([IO.File]::ReadAllText($webappConfigPath) | ConvertFrom-Json).skills_dir)
    } catch {
        return @{ State = 'unknown'; Why = 'webapp_config.json did not parse' }
    }
    $value = $value.Trim()
    if (-not $value) { return @{ State = 'empty' } }
    if (-not [IO.Path]::IsPathRooted($value)) { return @{ State = 'unknown'; Why = "skills_dir is not an absolute path: $value" } }
    return @{ State = 'set'; Path = $value }
}

# Add one key Lite reads to webapp_config.json, only when it is missing, after a
# backup. The text is spliced in after the opening brace, so every byte the owner
# wrote stays as it was; the result must parse before anything is written.
function Add-MissingConfigKey([string]$key, [string]$jsonValue) {
    if (-not (Test-Path -LiteralPath $webappConfigPath -PathType Leaf)) {
        Write-Host "[ok] no webapp_config.json yet: $key starts empty, nothing to add"
        return
    }
    $raw = [IO.File]::ReadAllText($webappConfigPath)
    try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null }
    if (-not ($parsed -is [pscustomobject])) {
        Write-Host "[unknown] webapp_config.json did not parse as an object: left untouched, $key not added -> $webappConfigPath"
        return
    }
    if (@($parsed.PSObject.Properties.Name) -contains $key) {
        Write-Host "[ok] unchanged: $key is already in webapp_config.json"
        return
    }
    $brace = $raw.IndexOf('{')
    $rest = $raw.Substring($brace + 1)
    $separator = if ($rest.Trim().StartsWith('}')) { '' } else { ',' }
    $updated = $raw.Substring(0, $brace + 1) + "`n  `"$key`": $jsonValue$separator" + $rest
    try { $null = $updated | ConvertFrom-Json } catch {
        Write-Host "[unknown] adding $key would not leave valid JSON: left untouched -> $webappConfigPath"
        return
    }
    $backup = "$webappConfigPath.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Invoke-Change "backup written -> $backup" { Copy-Item -LiteralPath $webappConfigPath -Destination $backup }
    Invoke-Change "key added to webapp_config.json: $key = $jsonValue" { [IO.File]::WriteAllText($webappConfigPath, $updated) }
}

# The skills folders install may write into: ~/.copilot/skills, plus
# ~/.claude/skills when it already exists as a real folder. A link is never
# written through.
function Get-IssueSkillDirs {
    $dirs = @($skillsDir)
    $claude = Get-Item -LiteralPath $claudeSkillsDir -Force -ErrorAction SilentlyContinue
    if (Test-Link $claude) {
        Write-Host "[skip] $claudeSkillsDir is a link into another tree, nothing written inside it ($([string]($claude.Target | Select-Object -First 1)))"
    } elseif ($claude) {
        $dirs += $claudeSkillsDir
    }
    return $dirs
}

# Remove the recorded skills_dir links under $dir (skills_dir was cleared or
# uninstall), leaving any entry that no longer matches its record alone.
function Remove-RecordedLinks([string]$dir, $links) {
    $kept = @()
    foreach ($link in $links) {
        if (-not ([string]$link.path).StartsWith($dir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { $kept += $link; continue }
        $item = Get-Item -LiteralPath $link.path -Force -ErrorAction SilentlyContinue
        if (Find-RecordedLink $links $link.path) {
            Invoke-Change "skills_dir link removed -> $($link.path)" { $item.Delete() }
        } elseif ($item) {
            Write-Host "[ok] kept (not Lite's: no longer the link it recorded) -> $($link.path)"
        }
    }
    return ,$kept
}

# Link the four issue skills from skills_dir into $dir. Returns the updated record.
function Sync-IssueSkillLinks([string]$dir, [string]$folder, $links) {
    $result = [Collections.ArrayList]@($links)
    foreach ($name in $issueSkills) {
        $link = Join-Path $dir $name
        $target = Join-Path $folder $name
        $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
        $recorded = Find-RecordedLink $result $link
        if (-not $recorded) {
            # A record for this path whose link is gone or was replaced no longer names anything of Lite's.
            foreach ($stale in @($result | Where-Object { Test-SamePath $_.path $link })) { $result.Remove($stale) }
        }
        if ($recorded -and (Test-SamePath $recorded.target $target)) {
            Write-Host "[ok] unchanged -> $link"
            continue
        }
        if ($recorded) {
            Invoke-Change "earlier skills_dir link removed -> $link" { $item.Delete() }
            $result.Remove($recorded)
        } elseif ($item -and (Test-OurSkill $item)) {
            Invoke-Change "Lite's bundled $name unlinked (skills_dir provides it) -> $link" { Remove-SkillEntry $item }
        } elseif ($item) {
            Write-Host "[ok] kept (not Lite's), and it counts as $name -> $link"
            continue
        }
        if (-not (Test-Path -LiteralPath (Join-Path $target 'SKILL.md') -PathType Leaf)) {
            Write-Host "[missing] $name has no SKILL.md in the skills folder, not linked -> $target"
            continue
        }
        Invoke-Change "issue skill linked -> $link ($target)" { New-Item -ItemType Junction -Path $link -Target $target | Out-Null }
        if (-not $WhatIf) { [void]$result.Add(@{ path = $link; target = $target }) }
    }
    return ,$result
}

$copilotHomeLink = Get-ForeignLink $copilotHome
if ($copilotHomeLink) {
    Write-Host "[skip] Copilot home is a link into another tree, nothing written -> $copilotHome ($copilotHomeLink)"
    exit 0
}

$hooksLink = Get-ForeignLink $hooksDir
$skillsItem = Get-Item -LiteralPath $skillsDir -Force -ErrorAction SilentlyContinue
$skillsLink = Get-ForeignLink $skillsDir

# --- uninstall: remove only what carries Lite's marker, leave hooks\state alone
if ($Uninstall) {
    if ($hooksLink) {
        Write-Host "[skip] hooks folder is a link into another tree, no hook removed -> $hooksDir ($hooksLink)"
    } else {
        foreach ($hook in $hookConfigs) {
            $target = Join-Path $hooksDir $hook.File
            if (-not (Test-Path -LiteralPath $target)) {
                Write-Host "[ok] not installed -> $target"
            } elseif (Test-OurHook $target $hook.Script) {
                Invoke-Change "hook config removed -> $target" { Remove-Item -LiteralPath $target -Force }
            } else {
                Write-Host "[skip] hook config is not ours (it does not run this checkout's hooks) -> $target"
            }
        }
    }
    if (-not $skillsItem) {
        Write-Host "[ok] no skills folder -> $skillsDir"
    } elseif ($skillsLink) {
        Write-Host "[skip] skills dir is a link into another tree, not touched -> $skillsDir ($skillsLink)"
    } else {
        $removed = 0
        foreach ($entry in @(Get-ChildItem -LiteralPath $skillsDir -Directory -Force)) {
            if (Test-OurSkill $entry) {
                Invoke-Change "skill removed -> $($entry.FullName)" { Remove-SkillEntry $entry }
                $removed++
            }
        }
        if ($removed -eq 0) { Write-Host "[ok] no skill of ours in $skillsDir" }
    }
    $record = @(Read-InstallRecord)
    if ($record.Count -gt 0) {
        $left = $record
        foreach ($dir in @(Get-IssueSkillDirs)) {
            if ((Get-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue) -and -not (Get-ForeignLink $dir)) {
                $left = Remove-RecordedLinks $dir $left
            }
        }
        # Uninstall is done with every recorded link, removed or kept, so the record goes.
        Save-InstallRecord @() $true
    }
    Write-Host ''
    if ($WhatIf) { Write-Host 'WhatIf: nothing was changed.' }
    Write-Host "Done. hooks\state was not touched: $stateDir"
    Write-Host 'Restart any running Copilot CLI session so it drops the hooks.'
    exit 0
}

$sharedWhy = $null
foreach ($candidate in @($skillsDir, $agentsSkillsDir)) {
    $link = Get-ForeignLink $candidate
    if ($link) { $sharedWhy = "$candidate -> $link"; break }
}
$shared = [bool]$sharedWhy
if ($shared) {
    Write-Host "[note] shared machine (the full fleet runs here too): $sharedWhy"
}

# --- the old fleet-config-lite install: its two hook configs and its skill junctions
$oldHooks = @()
if (-not $hooksLink) {
    foreach ($name in $oldHookFiles) {
        $path = Join-Path $hooksDir $name
        if (Test-Path -LiteralPath $path -PathType Leaf) { $oldHooks += $path }
    }
}
$oldSkills = @()
if ($skillsItem -and -not $skillsLink) {
    foreach ($entry in @(Get-ChildItem -LiteralPath $skillsDir -Directory -Force)) {
        $target = [string]($entry.Target | Select-Object -First 1)
        if (($entry.LinkType -eq 'Junction') -and ($target -match $oldSkillTarget)) { $oldSkills += $entry }
    }
}

if ($Migrate) {
    if ($oldHooks.Count -eq 0 -and $oldSkills.Count -eq 0) {
        Write-Host '[ok] no fleet-config-lite hook config or skill link to remove'
    } else {
        Write-Host 'Migrating off fleet-config-lite. These would be removed (hooks\state stays):'
        foreach ($path in $oldHooks) { Write-Host "  hook config -> $path" }
        foreach ($entry in $oldSkills) { Write-Host "  skill link  -> $($entry.FullName)" }
        if (-not (Confirm-Step 'Remove them and install this checkout''s hooks and skills?')) {
            Write-Host '[skip] migration declined: nothing was changed'
            exit 1
        }
        foreach ($path in $oldHooks) {
            Invoke-Change "old hook config removed -> $path" { Remove-Item -LiteralPath $path -Force }
        }
        foreach ($entry in $oldSkills) {
            Invoke-Change "old skill link removed -> $($entry.FullName)" { Remove-SkillEntry $entry }
        }
    }
    $oldInstructions = Join-Path $copilotHome 'copilot-instructions.md'
    $instructionsItem = Get-Item -LiteralPath $oldInstructions -Force -ErrorAction SilentlyContinue
    if ($instructionsItem) {
        $instructionsTarget = [string]($instructionsItem.Target | Select-Object -First 1)
        $oldMarker = ($instructionsItem.LinkType -ne 'SymbolicLink') -and (([IO.File]::ReadAllText($oldInstructions)) -match '^<!-- fleet-config-lite: managed copy')
        if ($oldMarker -or ($instructionsTarget -match '[\\/]fleet-config-lite[^\\/]*[\\/]')) {
            Write-Host "[note] copilot-instructions.md came from fleet-config-lite and is left in place: this checkout does not manage global instructions -> $oldInstructions"
        }
    }
} elseif ($oldHooks.Count -gt 0) {
    Write-Host '[warn] the old fleet-config-lite hook configs are still installed: two session-state hooks race on the same state file.'
    Write-Host '       Stop the chief at a turn end, then re-run with -Migrate:'
    foreach ($path in $oldHooks) { Write-Host "         $path" }
}

# --- resolve a real python.exe (avoid the WindowsApps alias, which can hang
# --- when spawned non-interactively from a hook)
$python = $null
$candidates = @()
if ($env:LOCALAPPDATA) {
    $candidates += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter python.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
}
$fromPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($fromPath) { $candidates += $fromPath }
foreach ($candidate in $candidates) {
    if ($candidate -and ($candidate -notmatch '\\WindowsApps\\')) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error 'No usable python.exe found (only the WindowsApps alias). Install Python and re-run.'
}

# --- render each hook config with absolute paths (forward slashes keep the
# --- JSON free of escaping headaches). session-state feeds the Board; chief-guard
# --- is the chief's handover, turn-end holds and ask_user deny.
if ($hooksLink) {
    Write-Host "[skip] hooks folder is a link into another tree, no hook written -> $hooksDir ($hooksLink)"
} else {
    foreach ($dir in @($hooksDir, $stateDir)) {
        # Creating the folder is all: hooks\state's contents are never read or touched.
        if (-not (Test-Path -LiteralPath $dir)) {
            Invoke-Change "folder created -> $dir" { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        }
    }
    $scope = if ($shared) { ' --lite-sessions-only' } else { '' }
    foreach ($hook in $hookConfigs) {
        $target = Join-Path $hooksDir $hook.File
        $targetLink = Get-ForeignLink $target
        if ($targetLink) {
            Write-Host "[skip] hook config is a link into another tree -> $target ($targetLink)"
            continue
        }
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and -not (Test-OurHook $target $hook.Script)) {
            Write-Host "[ok] kept (not Lite's: it does not run this checkout's hooks) -> $target"
            continue
        }
        $template = Get-Content (Join-Path $copilotRepo "hooks\$($hook.Template)") -Raw
        $rendered = $template.Replace('{{PYTHON}}', ($python -replace '\\', '/')).Replace('{{REPO}}', ($copilotRepo -replace '\\', '/')).Replace('{{SCOPE}}', $scope)
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and ([IO.File]::ReadAllText($target) -ceq $rendered)) {
            Write-Host "[ok] unchanged -> $target"
            continue
        }
        # WriteAllText, not Set-Content: under Windows PowerShell 5.1 Set-Content's
        # utf8 writes a BOM, and Copilot CLI 1.0.70 silently ignores BOM'd hook
        # configs (verified live). WriteAllText emits BOM-less UTF-8 on every PS.
        Invoke-Change "hook config -> $target" { [System.IO.File]::WriteAllText($target, $rendered) }
    }
    if ($shared) {
        Write-Host '     session-state records only sessions app-launcher-lite launched (--lite-sessions-only);'
        Write-Host '     chief-guard acts only for lite''s chief and its lanes'
    }
}
Write-Host "     python      -> $python"

# --- skills
# Never overwrite a skill this checkout does not own: on a machine where another
# setup already provides issue-* skills (e.g. one running the full fleet-config),
# an existing entry that is not ours is skipped.
#
# The skills dir itself must be a real folder. On a full-fleet machine
# ~/.copilot/skills is a junction into the full fleet-config's skills/, so a
# link created "in" it lands inside that other repo and every agent on the
# machine loads the lite copy beside the full one. A reparse-point skills dir
# is therefore never written into.
#
# The four issue skills come from exactly one place: skills_dir when it is set,
# else the bundled copies. A skills_dir that cannot be read leaves those four
# names as they are rather than guess.
Add-MissingConfigKey 'skills_dir' '""'
$skillSetting = Get-SkillsDirSetting
switch ($skillSetting.State) {
    'set' { Write-Host "[note] issue skills come from skills_dir -> $($skillSetting.Path)" }
    'unknown' { Write-Host "[unknown] skills_dir not read ($($skillSetting.Why)): the four issue skills are left as they are" }
    default { Write-Host '[note] skills_dir is empty: issue skills are Lite''s bundled copies' }
}
$record = @(Read-InstallRecord)
$recordBefore = ConvertTo-Json -InputObject @($record) -Depth 4 -Compress
if ($shared) {
    # Junctioned skills appear in every Copilot session, next to the full fleet's own.
    Write-Host "[skip] skills not installed on a shared machine -> $skillsDir"
    $skillSources = @()
} elseif ($skillsLink) {
    Write-Host "[skip] skills dir is a link into another tree, not touched -> $skillsDir ($skillsLink)"
    $skillSources = @()
} else {
    if (-not (Test-Path -LiteralPath $skillsDir)) {
        Invoke-Change "folder created -> $skillsDir" { New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null }
    }
    foreach ($dir in @(Get-IssueSkillDirs)) {
        if ($skillSetting.State -eq 'set') {
            $record = Sync-IssueSkillLinks $dir $skillSetting.Path $record
        } elseif ($skillSetting.State -eq 'empty') {
            $record = Remove-RecordedLinks $dir $record
        }
    }
    $skillSources = @(Get-ChildItem -Path $ourSkills -Directory)
    if ($skillSetting.State -ne 'empty') {
        $skillSources = @($skillSources | Where-Object { $issueSkills -notcontains $_.Name })
    }
}
foreach ($source in $skillSources) {
    $link = Join-Path $skillsDir $source.Name
    $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
    if ($item) {
        if (-not (Test-OurSkill $item)) {
            $counts = if ($issueSkills -contains $source.Name) { ", and it counts as $($source.Name)" } else { '' }
            Write-Host "[ok] kept (not Lite's)$counts -> $link"
            continue
        }
        if ((Test-Link $item) -and (Test-SamePath ([string]($item.Target | Select-Object -First 1)) $source.FullName)) {
            Write-Host "[ok] unchanged -> $link"
            continue
        }
        Invoke-Change "stale skill entry of Lite's removed -> $link" { Remove-SkillEntry $item }
    }
    if ($WhatIf) {
        Write-Host "[whatif] skill junction -> $link"
        continue
    }
    try {
        New-Item -ItemType Junction -Path $link -Target $source.FullName | Out-Null
        Write-Host "[ok] skill junction -> $link"
    } catch {
        Copy-Item -LiteralPath $source.FullName -Destination $link -Recurse
        [IO.File]::WriteAllText((Join-Path $link $skillMarker), 'app-launcher-lite copilot\install.ps1 manages this copy; -Uninstall removes it')
        Write-Host "[ok] skill copied   -> $link (junction unavailable)"
    }
}
Save-InstallRecord $record ((ConvertTo-Json -InputObject @($record) -Depth 4 -Compress) -cne $recordBefore)

# --- Migrate: the Copilot folder setting. fleet_config_dir defaults to this
# --- checkout's copilot folder only while it is empty; an explicit path (the old
# --- fleet-config-lite folder) keeps the chief there, where its guard hooks and
# --- the launcher's own check no longer recognise it. Lite's Settings API changes
# --- the one field; an existing value is never rewritten in the file behind a
# --- running webapp.
function Update-CopilotFolderSetting {
    $configPath = Join-Path $repo 'config\webapp_config.json'
    if (-not (Test-Path -LiteralPath $configPath)) {
        Write-Host '[ok] Copilot folder setting: no webapp_config.json, so Lite uses this checkout''s copilot folder'
        return
    }
    try {
        $current = [string](([IO.File]::ReadAllText($configPath) | ConvertFrom-Json).fleet_config_dir)
    } catch {
        Write-Host "[unknown] Copilot folder setting not read (webapp_config.json did not parse): check it in Lite's Settings -> Folders"
        return
    }
    if (-not $current.Trim()) {
        Write-Host '[ok] Copilot folder setting is empty: Lite uses this checkout''s copilot folder'
        return
    }
    if (Test-SamePath $current $copilotRepo) {
        Write-Host "[ok] Copilot folder setting already points here -> $current"
        return
    }
    Write-Host "[note] Lite's Copilot folder setting holds another folder -> $current"
    Write-Host '       A chief started there is not recognised by this checkout. Pointing it at the copilot folder here:'
    Write-Host "       $copilotRepo"
    if (-not (Confirm-Step 'Change that one Lite setting now, through its Settings API?')) {
        Write-Host '[skip] Copilot folder setting left as it was; set it in Lite''s Settings -> Folders before starting the chief'
        return
    }
    $url = [string](& $python (Join-Path $copilotRepo 'skills\_lib\lite_ports.py') webapp-url 2>$null | Select-Object -First 1)
    if ($url -notmatch '^http://127\.0\.0\.1:\d+$') {
        Write-Host '[unknown] Lite''s webapp address not read (lite_ports.py did not answer): set the Copilot folder in Settings -> Folders'
        return
    }
    # An explicit path, not an empty value: the running webapp keeps whatever it is
    # sent until its next start, and an empty folder there would read as the current directory.
    $body = (@{ fleet_config_dir = $copilotRepo } | ConvertTo-Json -Compress)
    if ($WhatIf) {
        Write-Host "[whatif] Copilot folder setting -> $copilotRepo (through $url/api/config)"
        return
    }
    try {
        Invoke-RestMethod -Uri "$url/api/config" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10 | Out-Null
        $after = [string](Invoke-RestMethod -Uri "$url/api/config" -Method Get -TimeoutSec 10).fleet_config_dir
    } catch {
        Write-Host "[unknown] Lite did not take the change ($url): start it (tray.bat), then re-run copilot\install.ps1 -Migrate, or set the Copilot folder in Settings -> Folders"
        return
    }
    if (Test-SamePath $after $copilotRepo) {
        Write-Host "[ok] Copilot folder setting -> $after (read back from Lite)"
    } else {
        Write-Host "[unknown] Lite answered but its Copilot folder reads back as '$after': check Settings -> Folders"
    }
}
if ($Migrate) {
    Update-CopilotFolderSetting
    Write-Host '[next] restart Lite (tray.bat --restart), then start the chief from the Board'
}

# --- fresh-machine checklist: every line is ok, missing or unknown, never assumed
if (-not $SkipChecklist) {
    Write-Host ''
    Write-Host 'Fresh-machine checklist:'
    $copilot = Get-Command copilot -ErrorAction SilentlyContinue
    if ($copilot) {
        $version = (& $copilot.Source --version 2>$null | Select-Object -First 1)
        Write-Host "[ok] Copilot CLI -> $version"
    } else {
        Write-Host '[missing] Copilot CLI: install it (winget install GitHub.Copilot), then run copilot login'
    }
    # The GitLab host is the one the git-access check below asks about: the https GitLab
    # remote of this checkout or a sibling. glab keeps one sign-in per host, so a bare
    # `glab auth status` answers for glab's default host and misreports a self-hosted one.
    $accessScript = Join-Path $copilotRepo 'skills\_lib\gitlab_git_access.py'
    $access = @{}
    $fixes = @()
    foreach ($line in @(& $python $accessScript check --repo $repo 2>$null)) {
        $key, $value = ([string]$line) -split '=', 2
        if ($key -eq 'FIX') { $fixes += $value } else { $access[$key] = $value }
    }
    $gitlabHost = $access['HOST']
    $glab = Get-Command glab -ErrorAction SilentlyContinue
    if (-not $glab) {
        Write-Host '[missing] glab: install it (winget install GLab.GLab), then run glab auth login'
    } elseif (-not $gitlabHost) {
        Write-Host '[unknown] glab sign-in not confirmed: no GitLab host is configured (no https GitLab remote found in this checkout or its sibling checkouts)'
    } else {
        # glab prints its status on stderr; under 'Stop', Windows PowerShell 5.1 turns that
        # into a terminating NativeCommandError and the checklist dies here.
        $ErrorActionPreference = 'Continue'
        & $glab.Source auth status --hostname $gitlabHost *> $null
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -eq 0) { Write-Host "[ok] glab signed in -> $gitlabHost" } else { Write-Host "[missing] glab is not signed in to ${gitlabHost}: run glab auth login --hostname $gitlabHost" }
    }
    # glab signed in does not mean git can reach GitLab: git asks its own credential
    # helper, and a lane (prompts off) stops on its first fetch. Ask git itself. The
    # fix is printed for the owner to run; this installer never edits git config.
    switch ($access['GIT_ACCESS']) {
        'ok' { Write-Host "[ok] git reaches GitLab without a prompt -> $($access['HOST'])" }
        'missing' {
            Write-Host "[missing] git cannot reach GitLab without a prompt ($($access['HOST'])): $($access['REASON'])"
            Write-Host '          glab being signed in is not enough: git uses its own credential helper. Run this yourself (the installer never edits your git config):'
            foreach ($fix in $fixes) { Write-Host "            $fix" }
            Write-Host '          then re-run install.ps1; this line should read [ok]'
        }
        default {
            $why = if ($access['REASON']) { $access['REASON'] } else { 'the check did not run' }
            Write-Host "[unknown] git access to GitLab not confirmed: $why"
        }
    }
    # The helpers run through the repo-root venv that setup.bat creates; the chief skips them without it.
    if (Test-Path (Join-Path $repo '.venv\Scripts\python.exe')) { Write-Host '[ok] venv' } else { Write-Host '[missing] venv: run setup.bat' }
    # The session-host port is config\config.json's; lite_ports.py is the one reader every caller shares.
    $portsScript = Join-Path $copilotRepo 'skills\_lib\lite_ports.py'
    $hostUrl = [string](& $python $portsScript session-host-url 2>$null | Select-Object -First 1)
    if ($hostUrl -notmatch '^http://127\.0\.0\.1:(\d+)/sessions$') {
        Write-Host '[unknown] session-host port not read (lite_ports.py did not answer)'
    } else {
        $hostPort = $Matches[1]
        try {
            Invoke-WebRequest -Uri $hostUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
            Write-Host "[ok] session-host answers on :$hostPort"
        } catch {
            Write-Host "[unknown] session-host not reachable on :$hostPort (start the tray)"
        }
    }
    Write-Host '[next] restart any running Copilot CLI session, then run: python copilot\scripts\copilot_probe.py run --live'
}

Write-Host ''
if ($WhatIf) { Write-Host 'WhatIf: nothing was changed. Run again without -WhatIf to make the changes listed above.' }
Write-Host 'Done. Restart any running Copilot CLI session to pick up the hooks.'
Write-Host "State file will appear at: $stateDir\sessions-state.json"
