// Service Bus - Standard tier (topics/subscriptions + duplicate detection).
// Local keys disabled: services authenticate with managed identity only.
param location string
param name string
param topology object
param principalId string

resource ns 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: name
  location: location
  sku: { name: 'Standard', tier: 'Standard' }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource topics 'Microsoft.ServiceBus/namespaces/topics@2022-10-01-preview' = [for t in items(topology): {
  parent: ns
  name: t.key
  properties: {
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    defaultMessageTimeToLive: 'P7D'
    supportOrdering: false
  }
}]

var subscriptionList = flatten(map(items(topology), t => map(t.value, s => { topic: t.key, sub: s })))

resource subs 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2022-10-01-preview' = [for s in subscriptionList: {
  name: '${name}/${s.topic}/${s.sub}'
  properties: {
    maxDeliveryCount: 5                    // then -> dead-letter queue
    lockDuration: 'PT30S'                  // peek-lock
    deadLetteringOnMessageExpiration: true
  }
  dependsOn: [ topics ]
}]

var dataOwner = '090c5cfd-751d-490a-894a-3ce6f1109419'

resource role 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: ns
  name: guid(ns.id, principalId, dataOwner)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dataOwner)
  }
}

output fqdn string = '${name}.servicebus.windows.net'
