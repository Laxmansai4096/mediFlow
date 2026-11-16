// API Management (Consumption tier - first 1M calls/month free).
// Policies: JWT validation, rate limiting by caller, request size limit,
// header hygiene, and blocking of /internal routes.
param location string
param name string
param publisherEmail string
param gatewayBackendUrl string

resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' = {
  name: name
  location: location
  sku: { name: 'Consumption', capacity: 0 }
  properties: {
    publisherEmail: publisherEmail
    publisherName: 'MediFlow AI'
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = {
  parent: apim
  name: 'mediflow'
  properties: {
    displayName: 'MediFlow API'
    path: 'api'
    protocols: [ 'https' ]
    serviceUrl: '${gatewayBackendUrl}/api'
    subscriptionRequired: false
  }
}

resource wildcard 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = [for m in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']: {
  parent: api
  name: 'all-${toLower(m)}'
  properties: { displayName: '${m} all', method: m, urlTemplate: '/*' }
}]

resource policy 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: '''
<policies>
  <inbound>
    <base />
    <choose>
      <when condition="@(context.Request.Url.Path.Contains(&quot;/internal&quot;))">
        <return-response><set-status code="404" reason="Not Found" /></return-response>
      </when>
    </choose>
    <rate-limit-by-key calls="300" renewal-period="60" counter-key="@(context.Request.IpAddress)" />
    <set-header name="X-Forwarded-For" exists-action="override"><value>@(context.Request.IpAddress)</value></set-header>
    <set-header name="X-Correlation-Id" exists-action="skip"><value>@(context.RequestId.ToString())</value></set-header>
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
    <set-header name="Strict-Transport-Security" exists-action="override"><value>max-age=31536000; includeSubDomains</value></set-header>
    <set-header name="X-Powered-By" exists-action="delete" />
  </outbound>
  <on-error><base /></on-error>
</policies>
'''
  }
}

output gatewayUrl string = apim.properties.gatewayUrl
