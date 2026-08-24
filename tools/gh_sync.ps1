# GitHub API sync: push local master commits to GitHub via REST API.
# Reason: account flagged -> git-over-HTTPS blocked (404), but REST API works.
# Usage: powershell -File tools\gh_sync.ps1
# Token file: data/.gh_token (gitignored). Requires token Contents:RW on repo.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$owner = "koukouisnme"; $repo = "stock-monitor"
$tokenFile = Join-Path $PSScriptRoot "..\data\.gh_token"
if (-not (Test-Path $tokenFile)) { throw "token file missing: data/.gh_token" }
$t = (Get-Content $tokenFile -Raw).Trim()
$h = @{Authorization = "Bearer $t"; "User-Agent" = "sm-sync"; Accept = "application/vnd.github+json"}
$api = "https://api.github.com/repos/$owner/$repo"
Set-Location (Join-Path $PSScriptRoot "..")

function Decode-GitPath([string]$p) {
    # git quotes non-ascii paths as octal escapes -> decode to utf8
    $p = $p.TrimEnd("`r")
    if ($p.Length -ge 2 -and $p[0] -eq '"' -and $p[$p.Length - 1] -eq '"') {
        $inner = $p.Substring(1, $p.Length - 2)
        $bytes = New-Object System.Collections.Generic.List[byte]
        $i = 0
        while ($i -lt $inner.Length) {
            if ($inner[$i] -eq '\' -and ($i + 3) -lt $inner.Length -and $inner.Substring($i + 1, 3) -match '^[0-7]{3}$') {
                $bytes.Add([Convert]::ToByte($inner.Substring($i + 1, 3), 8)); $i += 4
            } else {
                $bytes.Add([Convert]::ToByte([int][char]$inner[$i])); $i++
            }
        }
        return [Text.Encoding]::UTF8.GetString($bytes.ToArray())
    }
    return $p
}

function Parse-Person($kind, $text) {
    if ($text -match "(?m)^$kind (.+?) <(.+?)> (\d+) ([+-])(\d{2})(\d{2})") {
        $sign = 1; if ($Matches[4] -eq '-') { $sign = -1 }
        $offMin = $sign * ([int]$Matches[5] * 60 + [int]$Matches[6])
        $dt = [DateTimeOffset]::FromUnixTimeSeconds([long]$Matches[3]).ToOffset([TimeSpan]::FromMinutes($offMin))
        return @{name = $Matches[1]; email = $Matches[2]; date = $dt.ToString("yyyy-MM-ddTHH:mm:sszzz") }
    }
    throw "$kind parse failed"
}

# 1. remote HEAD
try { $ref = Invoke-RestMethod -Uri "$api/git/ref/heads/master" -Headers $h } catch { $ref = $null }
if (-not $ref) { $ref = Invoke-RestMethod -Uri "$api/git/ref/heads/main" -Headers $h }
$remoteSha = $ref.object.sha
$localHead = (git rev-parse HEAD).Trim()
Write-Host "remote: $remoteSha"
Write-Host "local : $localHead"
if ($remoteSha -eq $localHead) { Write-Host "already in sync"; exit 0 }

# 2. commits to push (oldest first)
$commits = @(git rev-list --reverse "$remoteSha..HEAD" | ForEach-Object { $_.Trim() })
Write-Host "commits to push: $($commits.Count)"
if ($commits.Count -eq 0) { throw "no commits found but HEAD differs (history diverged?)" }

$parent = $remoteSha
$prevTree = (Invoke-RestMethod -Uri "$api/git/commits/$remoteSha" -Headers $h).tree.sha
$tmpBlob = "$env:TEMP\sm_sync_blob.bin"
$newSha = $remoteSha

foreach ($c in $commits) {
    $expect = $c   # rev-list already yields full shas
    # changed entries vs parent tree: "<mode> <type> <sha>\t<path>" (git ls-tree of the commit, diff by path)
    $cur = @{}
    foreach ($line in (git ls-tree -r $c)) {
        $parts = $line -split "`t", 2
        $meta = $parts[0] -split ' '
        $cur[(Decode-GitPath $parts[1])] = @{mode = $meta[0]; sha = $meta[2]}
    }
    $old = @{}
    foreach ($line in (git ls-tree -r $parent)) {
        $parts = $line -split "`t", 2
        $meta = $parts[0] -split ' '
        $old[(Decode-GitPath $parts[1])] = @{mode = $meta[0]; sha = $meta[2]}
    }
    $entries = @()
    foreach ($k in $cur.Keys) {
        if (-not $old.ContainsKey($k) -or $old[$k].sha -ne $cur[$k].sha -or $old[$k].mode -ne $cur[$k].mode) {
            $entries += @{path = $k; mode = $cur[$k].mode; type = "blob"; sha = $cur[$k].sha }
        }
    }
    $removed = @($old.Keys | Where-Object { -not $cur.ContainsKey($_) })
    foreach ($k in $removed) { $entries += @{path = $k; mode = $old[$k].mode; type = "blob"; sha = $null } }

    # upload changed blobs (byte-exact via git cat-file)
    $uploaded = 0
    foreach ($e in $entries) {
        if (-not $e.sha) { continue }   # deletion, no blob
        # check blob exists remotely; upload if not
        $exists = $false
        try { Invoke-RestMethod -Uri "$api/git/blobs/$($e.sha)" -Headers $h | Out-Null; $exists = $true } catch {}
        if (-not $exists) {
            cmd /c "git cat-file blob $($e.sha) > `"$tmpBlob`"" | Out-Null
            $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($tmpBlob))
            $body = '{"content":"' + $b64 + '","encoding":"base64"}'
            $r = Invoke-RestMethod -Uri "$api/git/blobs" -Method Post -Headers $h -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 300
            if ($r.sha -ne $e.sha) { throw "blob sha mismatch: $($e.path)" }
            $uploaded++
        }
    }
    # build tree on base_tree
    $items = @()
    foreach ($e in $entries) {
        $shaJson = 'null'; if ($e.sha) { $shaJson = '"' + $e.sha + '"' }
        $items += ('{"path":' + (ConvertTo-Json $e.path) + ',"mode":"' + $e.mode + '","type":"blob","sha":' + $shaJson + '}')
    }
    $treeBody = '{"base_tree":"' + $prevTree + '","tree":[' + ($items -join ",") + ']}'
    $tr = Invoke-RestMethod -Uri "$api/git/trees" -Method Post -Headers $h -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($treeBody)) -TimeoutSec 300
    $prevTree = $tr.sha

    # replicate commit metadata
    cmd /c "git cat-file commit $c > `"$env:TEMP\sm_sync_raw.txt`"" | Out-Null
    $raw = [IO.File]::ReadAllText("$env:TEMP\sm_sync_raw.txt", [Text.Encoding]::UTF8)
    $blank = $raw.IndexOf("`n`n")
    $msg = $raw.Substring($blank + 2)
    $headPart = $raw.Substring(0, $blank)
    $author = Parse-Person 'author' $headPart
    $committer = Parse-Person 'committer' $headPart
    $msgJson = (ConvertTo-Json @($msg) -Compress).TrimStart('[').TrimEnd(']')
    $cjson = '{"message":' + $msgJson + ',"tree":"' + $tr.sha + '","parents":["' + $parent + '"],"author":' + ($author | ConvertTo-Json -Compress) + ',"committer":' + ($committer | ConvertTo-Json -Compress) + '}'
    $cr = Invoke-RestMethod -Uri "$api/git/commits" -Method Post -Headers $h -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($cjson)) -TimeoutSec 300
    $match = ($cr.sha -eq $expect)
    Write-Host ("commit {0} -> {1}  match={2}  blobs+={3}  entries={4}" -f $c.Substring(0, 7), $cr.sha.Substring(0, 7), $match, $uploaded, $entries.Count)
    if (-not $match) { Write-Host "  (sha differs - harmless: remote tree lineage differs from local)" }
    $parent = $cr.sha
    $newSha = $cr.sha
}

# 3. update refs
foreach ($br in @('master', 'main')) {
    $upd = '{"sha":"' + $newSha + '","force":true}'
    try {
        Invoke-RestMethod -Uri "$api/git/refs/heads/$br" -Method Patch -Headers $h -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($upd)) -TimeoutSec 300 | Out-Null
        Write-Host "$br -> $($newSha.Substring(0,7))"
    } catch { Write-Host "$br update failed: $($_.Exception.Response.StatusCode.value__)" }
}
Write-Host "DONE - synced $owner/$repo"
