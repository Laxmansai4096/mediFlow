// Azure SQL - Entra-only authentication, serverless databases using the
// Azure SQL Database free offer (auto-pause when the monthly free limit is hit).
param location string
param serverName string
param adminGroupObjectId string
param adminGroupName string
param databases array

resource server 'Microsoft.Sql/servers@2023-05-01-preview' = {
  name: serverName
  location: location
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled' // private endpoint in prod
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: adminGroupName
      sid: adminGroupObjectId
      principalType: 'Group'
      tenantId: subscription().tenantId
    }
  }
}

resource allowAzure 'Microsoft.Sql/servers/firewallRules@2023-05-01-preview' = {
  parent: server
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource dbs 'Microsoft.Sql/servers/databases@2023-05-01-preview' = [for db in databases: {
  parent: server
  name: 'mediflow-${db}'
  location: location
  sku: { name: 'GP_S_Gen5', tier: 'GeneralPurpose', family: 'Gen5', capacity: 1 }
  properties: {
    autoPauseDelay: 60
    minCapacity: json('0.5')
    useFreeLimit: true
    freeLimitExhaustionBehavior: 'AutoPause'
    requestedBackupStorageRedundancy: 'Local'
  }
}]

resource tde 'Microsoft.Sql/servers/databases/transparentDataEncryption@2023-05-01-preview' = [for (db, i) in databases: {
  parent: dbs[i]
  name: 'current'
  properties: { state: 'Enabled' }
}]

output fqdn string = server.properties.fullyQualifiedDomainName
