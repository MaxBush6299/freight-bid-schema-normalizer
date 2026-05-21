@description('Base name used to build resource names.')
param baseName string

@description('Deployment environment name (dev, test, prod).')
param environmentName string

@description('Azure region for all resources.')
param location string

@description('Tags applied to created resources.')
param tags object = {}

@description('Name of the Foundry project to create under the account.')
param projectName string = 'proj-rxo'

@description('OpenAI model name to deploy. gpt-5.4 (2026-03-05) is GlobalStandard GA in eastus.')
param modelName string = 'gpt-5.4'

@description('OpenAI model version to deploy.')
param modelVersion string = '2026-03-05'

@description('Model deployment SKU. GlobalStandard is the default pay-as-you-go tier.')
@allowed([
  'GlobalStandard'
  'DataZoneStandard'
  'Standard'
  'GlobalProvisioned'
  'Provisioned'
])
param modelSkuName string = 'GlobalStandard'

@description('Tokens-per-minute capacity in thousands (1 = 1 000 TPM).')
param modelCapacity int = 10

// ── Account ──────────────────────────────────────────────────────────────────
// Azure AI Services (kind=AIServices) is the resource type backing Foundry.
// Name must be globally unique; use uniqueString like storage/kv.
var accountName = take('ais-${uniqueString(resourceGroup().id, baseName, environmentName)}', 64)

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    allowProjectManagement: true
  }
}

// ── Project ───────────────────────────────────────────────────────────────────
// A Foundry project is a child resource of the AI Services account.
resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  name: projectName
  parent: aiAccount
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

// ── Model Deployment ─────────────────────────────────────────────────────────
// Deploy an OpenAI model to the AI Services account so agents can reference it.
// The deployment name is the value the runtime passes as the `model` parameter.
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-04-01-preview' = {
  name: modelName
  parent: aiAccount
  sku: {
    name: modelSkuName
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output accountName string = aiAccount.name
output accountId string = aiAccount.id
output accountEndpoint string = aiAccount.properties.endpoint

// The Foundry project endpoint format used by FoundryAgentClient
output projectEndpoint string = '${aiAccount.properties.endpoint}api/projects/${projectName}'
output projectResourceId string = foundryProject.id
output projectName string = foundryProject.name
output modelDeploymentName string = modelDeployment.name
