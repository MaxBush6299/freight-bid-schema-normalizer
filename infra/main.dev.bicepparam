using './main.bicep'

param environmentName = 'dev'
param location = 'eastus'
param baseName = 'rxodocnorm'
param tags = {
  app: 'rxo-document-normalizer'
  environment: 'dev'
  owner: 'rxo-data-platform'
}

param plannerMode = 'live'
param runMode = 'execute_with_validation'

param foundryProjectEndpoint = ''
param foundryAgentName = 'RXO-Document-Normalizer'
param foundryAgentVersion = '5'
param foundryAssistantId = ''

param foundryPostProcessAgentName = 'RXO-Notes-PostProcessor'
param foundryPostProcessAgentVersion = '1'
param postprocessMode = 'mock'

param rehydratePlannerMode = 'mock'
param rehydrateFoundryMode = 'mock'

param foundryAccountName = ''
param foundryProjectName = 'proj-default'
param createFoundryProject = false
param enableFoundry = true
param foundryNewProjectName = 'proj-rxo'
param assignFoundryRoles = true

param enableWebApp = false
param webAppPlanSkuName = 'F1'
param webAppPlanSkuTier = 'Free'

param enableStreamlitContainerApp = false

param enableContainerWorker = false
param assignContainerWorkerRoles = false
param assignContainerWorkerFoundryRoles = false
