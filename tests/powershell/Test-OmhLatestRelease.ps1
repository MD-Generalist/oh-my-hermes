# Behavioural coverage for Resolve-OmhLatestReleaseLocation (issue #1953).
# Like Test-OmhRedirectLocation.ps1, this parses install.ps1 and evaluates only
# the function definitions: dot-sourcing the installer would execute its
# top-level installation flow. Invoke-WebRequest is replaced by a function,
# which PowerShell resolves before the cmdlet, so no request leaves the host.
param([switch]$InjectRegression)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$script:OmhMutationFailures = @()
$script:OmhWebReplies = @()
$script:OmhWebCalls = @()

$OmhLatestUrl = 'https://example.test/releases/latest'
$OmhTagUrl = 'https://example.test/releases/tag/v3.0.0'

function Invoke-WebRequest {
    [CmdletBinding()]
    param(
        [string]$Uri,
        [string]$Method,
        [int]$MaximumRedirection = -1,
        [switch]$UseBasicParsing
    )
    $script:OmhWebCalls += [pscustomobject]@{
        Uri = $Uri
        Method = $Method
        MaximumRedirection = $MaximumRedirection
        UseBasicParsing = [bool]$UseBasicParsing
    }
    if ($script:OmhWebCalls.Count -gt $script:OmhWebReplies.Count) {
        throw "unexpected request $($script:OmhWebCalls.Count) to $Uri"
    }
    return & $script:OmhWebReplies[$script:OmhWebCalls.Count - 1]
}

function New-OmhRedirectResponse {
    param([string]$Location)
    $headers = New-Object 'System.Collections.Generic.Dictionary[string,string]'
    $headers.Add('Location', $Location)
    return [pscustomobject]@{ Headers = $headers }
}

function Assert-OmhLatestRelease {
    param(
        [string]$Name,
        [scriptblock[]]$Replies,
        [string]$Expected,
        [int]$ExpectedCalls
    )

    $script:OmhWebReplies = $Replies
    $script:OmhWebCalls = @()
    try {
        $actual = Resolve-OmhLatestReleaseLocation $OmhLatestUrl
    } catch {
        $message = "$Name threw $($_.Exception.Message)"
        if ($InjectRegression) {
            # The injected resolver can only fail by reading the Response
            # property the exception does not have. Do not let unrelated
            # exceptions turn the mutation check green.
            if ($_.Exception.Message -notmatch "(?i)property 'Response'") {
                throw "Unexpected injected-resolver failure: $message"
            }
            $script:OmhMutationFailures += [pscustomobject]@{ Name = $Name; Message = $message }
            Write-Host "REGRESSION DETECTED: $message"
            return
        }
        throw $message
    }

    if ($actual -ne $Expected -or $script:OmhWebCalls.Count -ne $ExpectedCalls) {
        $message = "$Name returned '$actual' after $($script:OmhWebCalls.Count) request(s); expected '$Expected' after $ExpectedCalls."
        if ($InjectRegression) {
            $script:OmhMutationFailures += [pscustomobject]@{ Name = $Name; Message = $message }
            Write-Host "REGRESSION DETECTED: $message"
            return
        }
        throw $message
    }

    foreach ($call in $script:OmhWebCalls) {
        if ($call.Uri -ne $OmhLatestUrl -or $call.Method -ne 'Head' -or -not $call.UseBasicParsing) {
            throw "$Name sent $($call | ConvertTo-Json -Compress)."
        }
    }
    if ($ExpectedCalls -ge 1 -and $script:OmhWebCalls[0].MaximumRedirection -ne 0) {
        throw "$Name did not ask for the redirect itself first."
    }
    if ($ExpectedCalls -ge 2 -and $script:OmhWebCalls[1].MaximumRedirection -eq 0) {
        throw "$Name fallback did not follow the redirect."
    }

    Write-Host ('PASS: {0} ({1} request(s))' -f $Name, $ExpectedCalls)
}

try {
    $errors = $null
    $installerPath = (Resolve-Path (Join-Path $PSScriptRoot '..\..\install.ps1')).ProviderPath
    $installerAst = [System.Management.Automation.Language.Parser]::ParseFile(
        $installerPath, [ref]$null, [ref]$errors)
    if ($errors) {
        $errors | ForEach-Object {
            throw "install.ps1($($_.Extent.StartLineNumber),$($_.Extent.StartColumnNumber)): $($_.Message)"
        }
    }

    $OmhFunctionNames = @(
        'Get-OmhRedirectLocation',
        'Get-OmhPropertyValue',
        'Get-OmhFinalResponseUri',
        'Resolve-OmhLatestReleaseLocation'
    )
    foreach ($functionName in $OmhFunctionNames) {
        $definition = @($installerAst.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        }, $true))[0]
        if ($null -eq $definition) {
            throw "install.ps1 does not define $functionName."
        }
        # The AST extent contains just the function definition, not installer code.
        Invoke-Expression $definition.Extent.Text
    }

    if ($InjectRegression) {
        # Issue #1953 pre-fix resolver: it reads Exception.Response directly and
        # has no fallback, so a 5.1 NullReferenceException ends the installer.
        function Resolve-OmhLatestReleaseLocation {
            param([string]$Url)
            $OmhLatestLocation = ''
            try {
                $OmhLatestResponse = Invoke-WebRequest -Uri $Url -Method Head -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
                $OmhLatestLocation = Get-OmhRedirectLocation $OmhLatestResponse
            } catch {
                $OmhLatestError = $_.Exception.Response
                $OmhLatestLocation = Get-OmhRedirectLocation $OmhLatestError
            }
            return $OmhLatestLocation
        }
        Write-Host 'Injecting the pre-fix latest-release resolver.'
    }

    # Windows PowerShell 5.1 successful no-redirect response: the 302 comes
    # back as a response and its Location is read directly.
    Assert-OmhLatestRelease 'redirect returned as a response' @(
        { New-OmhRedirectResponse $OmhTagUrl }
    ) $OmhTagUrl 1

    # PowerShell 7 raises the 302 as an error that carries the response.
    Assert-OmhLatestRelease 'redirect raised with a Response' @(
        {
            $exception = New-Object System.Exception 'Response status code does not indicate success: 302 (Found).'
            $exception | Add-Member -NotePropertyName Response -NotePropertyValue (New-OmhRedirectResponse $OmhTagUrl)
            throw $exception
        }
    ) $OmhTagUrl 1

    # Issue #1953: Windows PowerShell 5.1 throws a NullReferenceException for
    # the no-redirect HEAD. It has no Response property, so the resolver must
    # follow the redirect and read HttpWebResponse.ResponseUri instead.
    Assert-OmhLatestRelease 'Windows PowerShell 5.1 NullReferenceException then ResponseUri' @(
        { throw (New-Object System.NullReferenceException) },
        { [pscustomobject]@{ BaseResponse = [pscustomobject]@{ ResponseUri = [uri]$OmhTagUrl } } }
    ) $OmhTagUrl 2

    # PowerShell 7's followed response names the final URL on the request it
    # sent: HttpResponseMessage.RequestMessage.RequestUri.
    Assert-OmhLatestRelease 'exception without Response then RequestMessage.RequestUri' @(
        { throw (New-Object System.InvalidOperationException 'no response') },
        {
            [pscustomobject]@{
                BaseResponse = [pscustomobject]@{
                    RequestMessage = [pscustomobject]@{ RequestUri = [uri]$OmhTagUrl }
                }
            }
        }
    ) $OmhTagUrl 2

    # A no-redirect response without a Location also takes the fallback.
    Assert-OmhLatestRelease 'response without Location then ResponseUri' @(
        { [pscustomobject]@{ Headers = (New-Object 'System.Collections.Generic.Dictionary[string,string]') } },
        { [pscustomobject]@{ BaseResponse = [pscustomobject]@{ ResponseUri = [uri]$OmhTagUrl } } }
    ) $OmhTagUrl 2

    # Every path failing yields '' so the installer prints its own
    # "could not resolve the latest release" diagnostic.
    Assert-OmhLatestRelease 'both requests fail' @(
        { throw (New-Object System.NullReferenceException) },
        { throw (New-Object System.Net.WebException 'offline') }
    ) '' 2
    Assert-OmhLatestRelease 'followed response without a final URI' @(
        { throw (New-Object System.NullReferenceException) },
        { [pscustomobject]@{ BaseResponse = [pscustomobject]@{} } }
    ) '' 2
    Assert-OmhLatestRelease 'followed response without BaseResponse' @(
        { throw (New-Object System.NullReferenceException) },
        { [pscustomobject]@{} }
    ) '' 2

    if ($InjectRegression) {
        $ps51Detector = @($script:OmhMutationFailures | Where-Object {
            $_.Name -eq 'Windows PowerShell 5.1 NullReferenceException then ResponseUri'
        })
        if ($ps51Detector.Count -eq 0) {
            throw 'Injected pre-fix resolver was not caught by the Windows PowerShell 5.1 NullReferenceException case.'
        }
        Write-Host 'PASS: Windows PowerShell 5.1 NullReferenceException caught the injected regression.'
        Write-Host "PASS: injected pre-fix resolver failed $($script:OmhMutationFailures.Count) of 8 cases as expected."
    } else {
        Write-Host 'PASS: 8 latest-release cases'
    }
} catch {
    [Console]::Error.WriteLine("FAIL: $($_.Exception.Message)")
    exit 1
}
