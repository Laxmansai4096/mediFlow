// PAID - Azure Health Data Services FHIR R4 service (deployFhir=true only).
param location string
param workspaceName string
param principalId string

resource ws 'Microsoft.HealthcareApis/workspaces@2023-11-01' = {
  name: workspaceName
  location: location
}

resource fhir 'Microsoft.HealthcareApis/workspaces/fhirservices@2023-11-01' = {
  parent: ws
  name: 'fhir'
  location: location
  kind: 'fhir-R4'
  identity: { type: 'SystemAssigned' }
  properties: {
    authenticationConfiguration: {
      authority: '${environment().authentication.loginEndpoint}${subscription().tenantId}'
      audience: 'https://${workspaceName}-fhir.fhir.azurehealthcareapis.com'
      smartProxyEnabled: false
    }
  }
}

var fhirContributor = '5a1fc7df-4bf1-4951-a576-89034ee01acd'

resource role 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: fhir
  name: guid(fhir.id, principalId, fhirContributor)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', fhirContributor)
  }
}
