@description('Base name used to build resource names.')
param baseName string

@description('Deployment environment name (dev, test, prod).')
param environmentName string

@description('Azure region for resources.')
param location string

@description('Tenant ID for the vault.')
param tenantId string = subscription().tenantId

@description('Tags applied to the key vault.')
param tags object = {}

var keyVaultName = toLower(take('kv-${uniqueString(resourceGroup().id, baseName, environmentName)}', 24))

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
    enabledForDeployment: true
    enabledForTemplateDeployment: true
    enabledForDiskEncryption: false
  }
}

output keyVaultId string = keyVault.id
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
