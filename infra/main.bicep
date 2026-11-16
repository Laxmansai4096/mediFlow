// MediFlow AI - Azure deployment (Infrastructure as Code)
//
// NOT DEPLOYED. This template documents the production architecture and is
// validated locally with `az bicep build` (no Azure calls, no cost).
//
// Cost guard rails: every paid AI resource is behind a flag that defaults to
// false, and the defaults choose free / consumption tiers wherever Azure has one.

targetScope = 'resourceGroup'

@description('Short prefix for resource names')
@maxLength(8)
param prefix string = 'mediflow'

param location string = resourceGroup().location

@allowed(['dev', 'prod'])
param environmentName string = 'dev'

@description('Container registry image prefix, e.g. myacr.azurecr.io/mediflow')
param imagePrefix string = 'ghcr.io/example/mediflow'
param imageTag string = 'latest'

@description('Entra ID group object id that administers Azure SQL')
param sqlAdminGroupObjectId string = '00000000-0000-0000-0000-000000000000'
param sqlAdminGroupName string = 'mediflow-sql-admins'

@description('PAID: Azure OpenAI + Document Intelligence. Leave false for zero AI cost (local engines are used).')
param deployAiServices bool = false

@description('PAID: Azure Health Data Services FHIR service.')
param deployFhir bool = false

@description('API Management Consumption tier (first 1M calls/month free).')
param deployApim bool = true

param publisherEmail string = 'admin@example.com'

var suffix = uniqueString(resourceGroup().id)
var names = {
  identity: '${prefix}-id-${suffix}'
  logs: '${prefix}-logs-${suffix}'
  appi: '${prefix}-appi-${suffix}'
  kv: take('${prefix}kv${suffix}', 24)
  sb: '${prefix}-sb-${suffix}'
  storage: take('${prefix}st${suffix}', 24)
  sql: '${prefix}-sql-${suffix}'
  env: '${prefix}-env-${suffix}'
  apim: '${prefix}-apim-${suffix}'
  aoai: '${prefix}-aoai-${suffix}'
  docintel: '${prefix}-di-${suffix}'
  search: '${prefix}-search-${suffix}'
  fhir: take('${prefix}hds${suffix}', 24)
}

// Topic -> subscriptions. Mirrors mediflow/common/bus.py TOPOLOGY exactly.
var topology = {
  'patient-events': ['profile', 'notification']
  'appointment-events': ['profile', 'notification', 'billing']
  'payment-events': ['notification', 'profile']
  'prescription-events': ['notification', 'profile']
  'document-events': ['document']
  'document-processed': ['profile']
  'reminder-events': ['notification']
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.identity
  location: location
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: { location: location, workspaceName: names.logs, appInsightsName: names.appi }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: { location: location, name: names.kv, principalId: uami.properties.principalId, workspaceId: monitoring.outputs.workspaceId }
}

module servicebus 'modules/servicebus.bicep' = {
  name: 'servicebus'
  params: { location: location, name: names.sb, topology: topology, principalId: uami.properties.principalId }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: { location: location, name: names.storage, principalId: uami.properties.principalId }
}

module sql 'modules/sql.bicep' = {
  name: 'sql'
  params: {
    location: location
    serverName: names.sql
    adminGroupObjectId: sqlAdminGroupObjectId
    adminGroupName: sqlAdminGroupName
    databases: ['identity', 'patient', 'appointment', 'prescription', 'billing', 'document', 'ai']
  }
}

module ai 'modules/ai.bicep' = if (deployAiServices) {
  name: 'ai'
  params: {
    location: location
    openAiName: names.aoai
    docIntelName: names.docintel
    searchName: names.search
    principalId: uami.properties.principalId
  }
}

module fhir 'modules/fhir.bicep' = if (deployFhir) {
  name: 'fhir'
  params: { location: location, workspaceName: names.fhir, principalId: uami.properties.principalId }
}

module apps 'modules/containerapps.bicep' = {
  name: 'containerapps'
  params: {
    location: location
    envName: names.env
    workspaceCustomerId: monitoring.outputs.workspaceCustomerId
    workspaceKey: monitoring.outputs.workspaceKey
    identityId: uami.id
    identityClientId: uami.properties.clientId
    imagePrefix: imagePrefix
    imageTag: imageTag
    serviceBusNamespace: servicebus.outputs.fqdn
    storageAccount: names.storage
    sqlServerFqdn: sql.outputs.fqdn
    keyVaultUri: keyvault.outputs.uri
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    aiEnabled: deployAiServices
    openAiEndpoint: deployAiServices ? ai!.outputs.openAiEndpoint : ''
    docIntelEndpoint: deployAiServices ? ai!.outputs.docIntelEndpoint : ''
    topology: topology
    environmentName: environmentName
  }
}

module apim 'modules/apim.bicep' = if (deployApim) {
  name: 'apim'
  params: { location: location, name: names.apim, publisherEmail: publisherEmail, gatewayBackendUrl: apps.outputs.gatewayUrl }
}

output gatewayUrl string = apps.outputs.gatewayUrl
output apimUrl string = deployApim ? apim!.outputs.gatewayUrl : ''
output serviceBusNamespace string = servicebus.outputs.fqdn
