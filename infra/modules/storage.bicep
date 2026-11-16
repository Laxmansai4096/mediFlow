// Blob Storage - medical documents (private) + immutable audit archive (WORM).
param location string
param name string
param principalId string

resource sa 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: name
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false   // Entra ID / managed identity only
    encryption: {
      keySource: 'Microsoft.Storage'
      services: { blob: { enabled: true } }
      requireInfrastructureEncryption: true
    }
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: sa
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
    isVersioningEnabled: true
  }
}

resource docs 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blob
  name: 'medical-documents'
  properties: { publicAccess: 'None' }
}

resource auditArchive 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blob
  name: 'audit-archive'
  properties: { publicAccess: 'None' }
}

resource worm 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-01-01' = {
  parent: auditArchive
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: 2555 // ~7 years
    allowProtectedAppendWrites: true
  }
}

var blobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource role 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sa
  name: guid(sa.id, principalId, blobContributor)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
  }
}
