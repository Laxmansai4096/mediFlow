// Key Vault - RBAC mode, purge protection, audit logs to Log Analytics.
// Holds: JWT signing key (local mode), payment webhook secret, storage key.
param location string
param name string
param principalId string
param workspaceId string

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled' // set 'Disabled' + private endpoint in prod
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

var kvSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, principalId, kvSecretsUser)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUser)
  }
}

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kv
  name: 'kv-audit'
  properties: {
    workspaceId: workspaceId
    logs: [ { categoryGroup: 'audit', enabled: true } ]
  }
}

output uri string = kv.properties.vaultUri
