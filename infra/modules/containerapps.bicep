// Azure Container Apps - one app per microservice, one per worker.
// APIs scale on HTTP concurrency; workers scale on Service Bus backlog via KEDA
// (azure-servicebus scaler, authenticated with the managed identity), down to 0.
param location string
param envName string
param workspaceCustomerId string
@secure()
param workspaceKey string
param identityId string
param identityClientId string
param imagePrefix string
param imageTag string
param serviceBusNamespace string
param storageAccount string
param sqlServerFqdn string
param keyVaultUri string
param appInsightsConnectionString string
param aiEnabled bool
param openAiEndpoint string
param docIntelEndpoint string
param topology object
param environmentName string

var services = ['identity', 'patient', 'appointment', 'prescription', 'billing', 'document', 'ai', 'audit']
var dbServices = ['identity', 'patient', 'appointment', 'prescription', 'billing', 'document', 'ai']

// worker -> [module, messages-per-replica, max replicas]
var workers = {
  notification: { module: 'mediflow.workers.notification_worker', target: '20', max: 10 }
  document: { module: 'mediflow.workers.document_worker', target: '10', max: 8 }
  profile: { module: 'mediflow.workers.profile_worker', target: '30', max: 6 }
  billing: { module: 'mediflow.workers.billing_worker', target: '30', max: 4 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: workspaceCustomerId, sharedKey: workspaceKey }
    }
    workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ]
  }
}

var commonEnv = [
  { name: 'MEDIFLOW_ENV', value: environmentName == 'prod' ? 'production' : 'dev' }
  { name: 'AZURE_CLIENT_ID', value: identityClientId }
  { name: 'MEDIFLOW_BUS_MODE', value: 'azure' }
  { name: 'MEDIFLOW_SERVICEBUS_NAMESPACE', value: serviceBusNamespace }
  { name: 'MEDIFLOW_STORAGE_MODE', value: 'azure' }
  { name: 'MEDIFLOW_STORAGE_ACCOUNT', value: storageAccount }
  { name: 'MEDIFLOW_AI_MODE', value: aiEnabled ? 'azure' : 'local' }
  { name: 'MEDIFLOW_EXTRACTION_MODE', value: aiEnabled ? 'azure' : 'local' }
  { name: 'MEDIFLOW_AOAI_ENDPOINT', value: openAiEndpoint }
  { name: 'MEDIFLOW_DOCINTEL_ENDPOINT', value: docIntelEndpoint }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'MEDIFLOW_JWT_SECRET', secretRef: 'jwt-secret' }
  { name: 'MEDIFLOW_PAYMENT_WEBHOOK_SECRET', secretRef: 'payment-webhook-secret' }
]
var serviceUrls = [for s in services: { name: 'MEDIFLOW_${toUpper(s)}_URL', value: 'http://${s}' }]

var kvSecrets = [
  { name: 'jwt-secret', keyVaultUrl: '${keyVaultUri}secrets/jwt-secret', identity: identityId }
  { name: 'payment-webhook-secret', keyVaultUrl: '${keyVaultUri}secrets/payment-webhook-secret', identity: identityId }
]

resource apis 'Microsoft.App/containerApps@2024-03-01' = [for s in services: {
  name: 'mf-${s}'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: false, targetPort: 8000, transport: 'http', allowInsecure: false }
      secrets: kvSecrets
    }
    template: {
      containers: [ {
        name: s
        image: '${imagePrefix}:${imageTag}'
        command: [ 'uvicorn', 'mediflow.services.${s}.main:create_app', '--factory', '--host', '0.0.0.0', '--port', '8000' ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: concat(commonEnv, serviceUrls, contains(dbServices, s) ? [
          { name: '${toUpper(s)}_DATABASE_URL', value: 'mssql+pyodbc://@${sqlServerFqdn}/mediflow-${s}?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi' }
        ] : [])
        probes: [
          { type: 'Liveness', httpGet: { path: '/health/live', port: 8000 } }
          { type: 'Readiness', httpGet: { path: '/health/ready', port: 8000 } }
        ]
      } ]
      scale: {
        minReplicas: 0
        maxReplicas: 10
        rules: [ { name: 'http', http: { metadata: { concurrentRequests: '50' } } } ]
      }
    }
  }
}]

resource gateway 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'mf-gateway'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'http', allowInsecure: false }
      secrets: kvSecrets
    }
    template: {
      containers: [ {
        name: 'gateway'
        image: '${imagePrefix}:${imageTag}'
        command: [ 'uvicorn', 'mediflow.services.gateway.main:create_app', '--factory', '--host', '0.0.0.0', '--port', '8000' ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: concat(commonEnv, serviceUrls)
      } ]
      scale: { minReplicas: 0, maxReplicas: 10, rules: [ { name: 'http', http: { metadata: { concurrentRequests: '100' } } } ] }
    }
  }
}

resource workerApps 'Microsoft.App/containerApps@2024-10-02-preview' = [for w in items(workers): {
  name: 'mf-worker-${w.key}'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: { secrets: kvSecrets }
    template: {
      containers: [ {
        name: 'worker'
        image: '${imagePrefix}:${imageTag}'
        command: [ 'python', '-m', w.value.module ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: concat(commonEnv, serviceUrls)
      } ]
      scale: {
        minReplicas: 0
        maxReplicas: w.value.max
        // one KEDA rule per topic this worker's subscription listens on
        rules: [for t in filter(items(topology), x => contains(x.value, w.key)): {
          name: 'sb-${t.key}'
          custom: {
            type: 'azure-servicebus'
            identity: identityId
            metadata: {
              namespace: split(serviceBusNamespace, '.')[0]
              topicName: t.key
              subscriptionName: w.key
              messageCount: w.value.target
            }
          }
        }]
      }
    }
  }
}]

output gatewayUrl string = 'https://${gateway.properties.configuration.ingress.fqdn}'
