targetScope = 'resourceGroup'

@description('Base name used to build resource names.')
param baseName string = 'rxodocnorm'

@description('Deployment environment name.')
@allowed([
  'dev'
  'test'
  'prod'
])
param environmentName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Tags applied across resources.')
param tags object = {}

@description('Blob containers to create.')
param containerNames array = [
  'input'
  'output'
  'artifacts'
  'export'
  'outbox'
  'templates'
]

@description('Create queue resources.')
param createQueues bool = false

@description('Queue names to create if createQueues is true.')
param queueNames array = []

@description('Storage SKU name.')
param storageSkuName string = 'Standard_LRS'

@description('Log Analytics retention in days.')
param workspaceRetentionInDays int = 30

@description('Function plan SKU name.')
param functionPlanSkuName string = 'FC1'

@description('Function plan SKU tier.')
param functionPlanSkuTier string = 'FlexConsumption'

@description('Function run mode.')
@allowed([
  'draft'
  'execute_with_validation'
])
param runMode string = 'execute_with_validation'

@description('Planner mode for function app.')
@allowed([
  'mock'
  'live'
])
param plannerMode string = 'live'

@description('Foundry project endpoint used by runtime.')
param foundryProjectEndpoint string = ''

@description('Foundry agent name used by runtime invocation only. Bicep does not create/manage Foundry agents.')
param foundryAgentName string = 'RXO-Document-Normalizer'

@description('Foundry agent version used by runtime invocation only. Bicep does not create/manage Foundry agents.')
param foundryAgentVersion string = '5'

@description('Foundry API version used by runtime.')
param foundryApiVersion string = '2025-05-15-preview'

@description('Foundry model fallback name used by runtime client.')
param foundryModel string = 'gpt-4.1'

@description('Canonical schema template name.')
param canonicalSchemaName string = 'freight_bid_v1'

@description('Foundry post-process agent name used by runtime only. Bicep does not create/manage Foundry agents.')
param foundryPostProcessAgentName string = 'RXO-Notes-PostProcessor'

@description('Foundry post-process agent version used by runtime only. Bicep does not create/manage Foundry agents.')
param foundryPostProcessAgentVersion string = '1'

@description('Post-process mode (mock | live).')
@allowed([
  'mock'
  'live'
])
param postprocessMode string = 'mock'

@description('Rehydrate (reverse pipeline) planner mode.')
@allowed([
  'mock'
  'live'
])
param rehydratePlannerMode string = 'mock'

@description('Foundry client mode for the Rehydrate LLM mapping service.')
@allowed([
  'mock'
  'live'
])
param rehydrateFoundryMode string = 'mock'

@description('Optional preconfigured Foundry assistant ID for runtime fallback. Bicep does not create/manage assistants.')
param foundryAssistantId string = ''

@description('Deploy a Web App to host the Streamlit companion UI.')
param enableWebApp bool = false

@description('Web App plan SKU name (F1 Free avoids Basic VM quota).')
param webAppPlanSkuName string = 'F1'

@description('Web App plan SKU tier.')
param webAppPlanSkuTier string = 'Free'

@description('Deploy Streamlit as an Azure Container App (no VM quota needed).')
param enableStreamlitContainerApp bool = true

@description('ACR login server hosting the Streamlit image.')
param acrLoginServer string = ''

@description('ACR admin username.')
@secure()
param acrUsername string = ''

@description('ACR admin password.')
@secure()
param acrPassword string = ''

@description('Streamlit container image (repo:tag).')
param streamlitContainerImage string = 'streamlit-ui:v1'

@description('Optional Foundry account name in this resource group.')
param foundryAccountName string = ''

@description('Optional Foundry project name under foundryAccountName.')
param foundryProjectName string = 'proj-default'

@description('Whether to create the Foundry project resource under an existing account.')
param createFoundryProject bool = false

@description('Deploy a dedicated Azure AI Foundry account and project via the foundry module.')
param enableFoundry bool = false

@description('Foundry project name to create when enableFoundry=true.')
param foundryNewProjectName string = 'proj-rxo'

@description('Whether to assign Foundry data-plane roles to the Function App identity.')
param assignFoundryRoles bool = false

@description('Whether to assign storage/key vault roles to the optional container worker identity.')
param assignContainerWorkerRoles bool = false

@description('Whether to assign Foundry data-plane roles to the optional container worker identity.')
param assignContainerWorkerFoundryRoles bool = false

@description('Enable optional container worker placeholder module.')
param enableContainerWorker bool = false

@description('Container image for optional execution worker job.')
param containerWorkerImage string = 'mcr.microsoft.com/k8se/quickstart-jobs:latest'

@description('CPU cores for optional execution worker job container.')
param containerWorkerCpu int = 1

@description('Memory allocation for optional execution worker job container.')
param containerWorkerMemory string = '2Gi'

@description('Max runtime in seconds for optional execution worker job replicas.')
param containerWorkerReplicaTimeoutSeconds int = 1200

@description('Max retry count for optional execution worker job replicas.')
param containerWorkerReplicaRetryLimit int = 1

@description('Non-secret environment variables for the optional execution worker container.')
param containerWorkerEnvironmentVariables object = {}

module storage './modules/storage.bicep' = {
  name: 'storage'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    skuName: storageSkuName
    containerNames: containerNames
    createQueues: createQueues
    queueNames: queueNames
    tags: tags
  }
}

// ── Foundry (optional dedicated AI Services account + project) ──
module foundry './modules/foundry.bicep' = if (enableFoundry) {
  name: 'foundry'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    projectName: foundryNewProjectName
    tags: tags
  }
}

// Resolved Foundry endpoint: prefer the dedicated module output, fall back to param
var resolvedFoundryEndpoint = enableFoundry
  ? foundry.?outputs.projectEndpoint ?? foundryProjectEndpoint
  : foundryProjectEndpoint

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    workspaceRetentionInDays: workspaceRetentionInDays
    tags: tags
  }
}

module keyVault './modules/keyVault.bicep' = {
  name: 'keyvault'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    tenantId: subscription().tenantId
    tags: tags
  }
}

module functionPlan './modules/functionPlan.bicep' = {
  name: 'functionplan'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    skuName: functionPlanSkuName
    skuTier: functionPlanSkuTier
    tags: tags
  }
}

var artifactContainerName = contains(containerNames, 'artifacts') ? 'artifacts' : containerNames[0]
var inputContainerName = contains(containerNames, 'input') ? 'input' : containerNames[0]
var outputContainerName = contains(containerNames, 'output') ? 'output' : containerNames[0]
var exportContainerName = contains(containerNames, 'export') ? 'export' : containerNames[0]
var outboxContainerName = contains(containerNames, 'outbox') ? 'outbox' : containerNames[0]
var templateContainerName = contains(containerNames, 'templates') ? 'templates' : containerNames[0]

module functionApp './modules/functionApp.bicep' = {
  name: 'functionapp'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    planId: functionPlan.outputs.planId
    storageAccountName: storage.outputs.storageAccountName
    storageBlobEndpoint: storage.outputs.primaryBlobEndpoint
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    keyVaultUri: keyVault.outputs.keyVaultUri
    inputContainer: inputContainerName
    outputContainer: outputContainerName
    artifactContainer: artifactContainerName
    additionalAppSettings: {
      RUN_MODE: runMode
      PLANNER_MODE: plannerMode
      FOUNDRY_PROJECT_ENDPOINT: resolvedFoundryEndpoint
      FOUNDRY_AGENT_NAME: foundryAgentName
      FOUNDRY_AGENT_VERSION: foundryAgentVersion
      FOUNDRY_API_VERSION: foundryApiVersion
      FOUNDRY_MODEL: foundryModel
      FOUNDRY_ASSISTANT_ID: foundryAssistantId
      FOUNDRY_POSTPROCESS_AGENT_NAME: foundryPostProcessAgentName
      FOUNDRY_POSTPROCESS_AGENT_VERSION: foundryPostProcessAgentVersion
      POSTPROCESS_MODE: postprocessMode
      CANONICAL_SCHEMA_NAME: canonicalSchemaName
      AZURE_BLOB_API_VERSION: '2021-08-06'
      ENABLE_LLM_VALIDATION: 'false'
      MAX_SCRIPT_EXECUTION_SECONDS: '45'
      MAX_PROFILE_SAMPLE_ROWS: '25'
      // Reverse pipeline (Rehydrate) settings
      EXPORT_CONTAINER: exportContainerName
      OUTBOX_CONTAINER: outboxContainerName
      TEMPLATE_CONTAINER: templateContainerName
      REHYDRATE_PLANNER_MODE: rehydratePlannerMode
      REHYDRATE_FOUNDRY_MODE: rehydrateFoundryMode
    }
    tags: tags
  }
}

// ── Web App Plan (separate from Function consumption plan) ──
module webAppPlan './modules/functionPlan.bicep' = if (enableWebApp) {
  name: 'webappplan'
  params: {
    baseName: '${baseName}web'
    environmentName: environmentName
    location: location
    skuName: webAppPlanSkuName
    skuTier: webAppPlanSkuTier
    planKind: 'linux'
    tags: tags
  }
}

// ── Streamlit Web App ──
module webApp './modules/webApp.bicep' = if (enableWebApp) {
  name: 'webapp'
  params: {
    baseName: baseName
    environmentName: environmentName
    location: location
    planId: webAppPlan.?outputs.?planId ?? ''
    storageAccountName: storage.outputs.storageAccountName
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    foundryProjectEndpoint: foundryProjectEndpoint
    functionAppHostname: functionApp.outputs.functionAppName
    tags: tags
  }
}

// ── Streamlit Container App (no VM quota needed) ──
module streamlitApp './modules/streamlitApp.bicep' = if (enableStreamlitContainerApp) {
  name: 'streamlitapp'
  params: {
    enabled: enableStreamlitContainerApp
    namePrefix: '${baseName}-streamlit-${environmentName}'
    location: location
    acrLoginServer: acrLoginServer
    acrUsername: acrUsername
    acrPassword: acrPassword
    containerImage: streamlitContainerImage
    storageAccountName: storage.outputs.storageAccountName
    foundryProjectEndpoint: foundryProjectEndpoint
    functionAppHostname: functionApp.outputs.functionAppName
    inputContainer: inputContainerName
    outputContainer: outputContainerName
    additionalEnvVars: {
      PLANNER_MODE: plannerMode
      RUN_MODE: runMode
      FOUNDRY_AGENT_NAME: foundryAgentName
      FOUNDRY_AGENT_VERSION: foundryAgentVersion
      FOUNDRY_API_VERSION: foundryApiVersion
      FOUNDRY_ASSISTANT_ID: foundryAssistantId
      FOUNDRY_POSTPROCESS_AGENT_NAME: foundryPostProcessAgentName
      FOUNDRY_POSTPROCESS_AGENT_VERSION: foundryPostProcessAgentVersion
      POSTPROCESS_MODE: postprocessMode
    }
    tags: tags
  }
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (createFoundryProject && !empty(foundryAccountName)) {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = if (createFoundryProject && !empty(foundryAccountName) && !empty(foundryProjectName)) {
  name: foundryProjectName
  parent: foundryAccount
  location: location
  properties: {
    displayName: foundryProjectName
    description: 'RXO Document Normalizer Foundry Project'
  }
}

// Resolved Foundry account/project names for RBAC: prefer new module, fall back to params
var resolvedFoundryAccountName = enableFoundry ? foundry.outputs.accountName : foundryAccountName
var resolvedFoundryProjectName = enableFoundry ? foundry.outputs.projectName : foundryProjectName

module roles './modules/roles.bicep' = {
  name: 'roles'
  params: {
    principalId: functionApp.outputs.principalId
    workerPrincipalId: containerWorker.outputs.workerPrincipalId
    webAppPrincipalId: webApp.?outputs.?principalId ?? ''
    streamlitAppPrincipalId: streamlitApp.?outputs.?streamlitPrincipalId ?? ''
    storageAccountName: storage.outputs.storageAccountName
    keyVaultName: keyVault.outputs.keyVaultName
    grantQueueRole: true
    assignFoundryRoles: assignFoundryRoles || enableFoundry
    assignWorkerRoles: assignContainerWorkerRoles
    assignWorkerFoundryRoles: assignContainerWorkerFoundryRoles
    assignWebAppRoles: enableWebApp
    assignWebAppFoundryRoles: enableWebApp && (assignFoundryRoles || enableFoundry)
    assignStreamlitAppRoles: enableStreamlitContainerApp
    assignStreamlitAppFoundryRoles: enableStreamlitContainerApp && (assignFoundryRoles || enableFoundry)
    foundryAccountName: resolvedFoundryAccountName
    foundryProjectName: resolvedFoundryProjectName
  }
}

module containerWorker './modules/containerWorker.bicep' = {
  name: 'containerworker'
  params: {
    enabled: enableContainerWorker
    namePrefix: 'worker-${baseName}-${environmentName}'
    location: location
    workerImage: containerWorkerImage
    workerCpu: containerWorkerCpu
    workerMemory: containerWorkerMemory
    replicaTimeoutSeconds: containerWorkerReplicaTimeoutSeconds
    replicaRetryLimit: containerWorkerReplicaRetryLimit
    workerEnvironmentVariables: containerWorkerEnvironmentVariables
    tags: tags
  }
}

output functionAppName string = functionApp.outputs.functionAppName
output functionAppId string = functionApp.outputs.functionAppId
output functionPrincipalId string = functionApp.outputs.principalId
output storageAccountName string = storage.outputs.storageAccountName
output storageBlobEndpoint string = storage.outputs.primaryBlobEndpoint
output keyVaultUri string = keyVault.outputs.keyVaultUri
output appInsightsConnectionString string = monitoring.outputs.appInsightsConnectionString
output foundryProjectResourceId string = createFoundryProject ? foundryProject.id : ''
output foundryAccountName string = enableFoundry ? foundry.?outputs.accountName ?? foundryAccountName : foundryAccountName
output foundryProjectEndpoint string = resolvedFoundryEndpoint
output containerWorkerJobResourceId string = containerWorker.outputs.workerJobResourceId
output containerWorkerManagedEnvironmentId string = containerWorker.outputs.workerManagedEnvironmentId
output containerWorkerPrincipalId string = containerWorker.outputs.workerPrincipalId
output webAppName string = webApp.?outputs.?webAppName ?? ''
output webAppDefaultHostname string = webApp.?outputs.?defaultHostname ?? ''
output webAppPrincipalId string = webApp.?outputs.?principalId ?? ''
output streamlitAppName string = streamlitApp.?outputs.?streamlitAppName ?? ''
output streamlitFqdn string = streamlitApp.?outputs.?streamlitFqdn ?? ''
output streamlitPrincipalId string = streamlitApp.?outputs.?streamlitPrincipalId ?? ''
