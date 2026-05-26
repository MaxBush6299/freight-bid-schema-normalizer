@description('Base name used to build resource names.')
param baseName string

@description('Deployment environment name (dev, test, prod).')
param environmentName string

@description('Azure region for resources.')
param location string

@description('App Service plan resource ID.')
param planId string

@description('Storage account name for managed-identity based access.')
param storageAccountName string

@description('Storage account primary blob endpoint.')
param storageBlobEndpoint string

@description('Application Insights connection string.')
param appInsightsConnectionString string

@description('Key Vault URI.')
param keyVaultUri string

@description('Input blob container name.')
param inputContainer string = 'input'

@description('Output blob container name.')
param outputContainer string = 'output'

@description('Artifact blob container name.')
param artifactContainer string = 'artifacts'

@description('Blob container used by Flex Consumption for deployment packages.')
param deploymentContainerName string = 'deploymentpackage'

@description('Maximum instance count for Flex Consumption scale-out.')
param maxInstanceCount int = 100

@description('Memory in MB allocated per instance.')
param instanceMemoryMB int = 2048

@description('Additional app settings merged into base Function app settings.')
param additionalAppSettings object = {}

@description('Tags applied to the Function App.')
param tags object = {}

var functionAppName = toLower(take('func-${uniqueString(resourceGroup().id, baseName, environmentName)}', 60))
var baseSettings = [
  {
    name: 'AzureWebJobsStorage__accountName'
    value: storageAccountName
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
  {
    name: 'INPUT_CONTAINER'
    value: inputContainer
  }
  {
    name: 'OUTPUT_CONTAINER'
    value: outputContainer
  }
  {
    name: 'FUNCTION_PERSIST_ARTIFACTS'
    value: 'true'
  }
  {
    name: 'ARTIFACT_STORAGE_MODE'
    value: 'blob'
  }
  {
    name: 'ARTIFACT_BLOB_CONTAINER'
    value: artifactContainer
  }
  {
    name: 'KEY_VAULT_URI'
    value: keyVaultUri
  }
]
var customSettings = [for item in items(additionalAppSettings): {
  name: item.key
  value: string(item.value)
}]

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: planId
    httpsOnly: true
    clientAffinityEnabled: false
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageBlobEndpoint}${deploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maxInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat(baseSettings, customSettings)
    }
  }
}

output functionAppId string = functionApp.id
output functionAppName string = functionApp.name
output principalId string = functionApp.identity.principalId
