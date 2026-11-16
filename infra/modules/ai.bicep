// PAID resources - only deployed when deployAiServices=true.
// Azure OpenAI (summaries, chat), Document Intelligence (extraction),
// Azure AI Search (Free tier) for RAG over the hospital knowledge base.
param location string
param openAiName string
param docIntelName string
param searchName string
param principalId string

resource aoai 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: openAiName
  location: location
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: openAiName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: aoai
  name: 'gpt-4o-mini'
  sku: { name: 'GlobalStandard', capacity: 10 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4o-mini', version: '2024-07-18' }
  }
}

resource docIntel 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: docIntelName
  location: location
  kind: 'FormRecognizer'
  sku: { name: 'F0' } // free tier: 500 pages/month
  properties: {
    customSubDomainName: docIntelName
    disableLocalAuth: true
  }
}

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  sku: { name: 'free' }
  properties: { replicaCount: 1, partitionCount: 1 }
}

var openAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var cogUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource r1 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aoai
  name: guid(aoai.id, principalId, openAiUser)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUser)
  }
}

resource r2 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: docIntel
  name: guid(docIntel.id, principalId, cogUser)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cogUser)
  }
}

output openAiEndpoint string = aoai.properties.endpoint
output docIntelEndpoint string = docIntel.properties.endpoint
output searchEndpoint string = 'https://${search.name}.search.windows.net'
