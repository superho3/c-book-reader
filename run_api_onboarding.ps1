param(
    [Parameter(Mandatory = $true)]
    [string]$ApiBaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiName,

    [Parameter(Mandatory = $true)]
    [string]$ApiDescription,

    [Parameter(Mandatory = $true)]
    [string]$SecurityGroup,

    [Parameter(Mandatory = $true)]
    [string]$BasePath,

    [Parameter(Mandatory = $true)]
    [string]$TechnicalContact,

    [Parameter(Mandatory = $true)]
    [string]$EamsId,

    [Parameter(Mandatory = $true)]
    [string]$Segment,

    [ValidateSet("REST", "SOAP", "GraphQL", "WebSocket", "A2A", "MCP")]
    [string]$Protocol = "REST",

    [ValidateSet("OWNERS", "REUSABLE")]
    [string]$IntendedUsage = "OWNERS",

    [string]$Environments = "PREPROD,PROD",

    [switch]$ApplicationFlow,

    [switch]$ValidateBasePath,

    [string]$FederatedSubject,
    [string]$FederatedIssuer,
    [string]$FederatedAudience
)

$token = (cldctl get azure-token --scope "api://c937a47e-c126-4da6-954f-16ecf188f3b7/.default").Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Failed to acquire Azure token from cldctl."
}

$headers = @{
    Authorization = "Bearer $token"
}

if ($ApplicationFlow -and $ValidateBasePath) {
    $validationUri = "$ApiBaseUrl/validations?basePath=$([uri]::EscapeDataString($BasePath))"
    $validationResult = Invoke-RestMethod -Method Get -Uri $validationUri -Headers $headers
    "BasePath validation result:"
    $validationResult | ConvertTo-Json -Depth 20
}

$environmentNames = $Environments.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
$environmentPayload = @()

foreach ($envName in $environmentNames) {
    $envObj = @{
        environmentName = $envName
    }

    if (
        -not [string]::IsNullOrWhiteSpace($FederatedSubject) -and
        -not [string]::IsNullOrWhiteSpace($FederatedIssuer) -and
        -not [string]::IsNullOrWhiteSpace($FederatedAudience)
    ) {
        $envObj.federatedCredential = @{
            subject  = $FederatedSubject
            issuer   = $FederatedIssuer
            audience = $FederatedAudience
        }
    }

    $environmentPayload += $envObj
}

$payloadObject = @{
    apiInformation = @{
        apiName          = $ApiName
        apiDescription   = $ApiDescription
        securityGroup    = $SecurityGroup
        basePath         = $BasePath
        technicalContact = $TechnicalContact
        eamsId           = $EamsId
        segment          = $Segment
        protocol         = $Protocol
        intendedUsage    = $IntendedUsage
    }
    environments = $environmentPayload
}

$payloadJson = $payloadObject | ConvertTo-Json -Depth 20
$endpoint = if ($ApplicationFlow) { "$ApiBaseUrl/onboarding/teams" } else { "$ApiBaseUrl/onboarding/users" }

$response = Invoke-RestMethod -Method Post -Uri $endpoint -Headers $headers -ContentType "application/json" -Body $payloadJson
"Onboarding response:"
$response | ConvertTo-Json -Depth 20
