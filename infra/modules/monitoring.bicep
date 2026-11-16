// Log Analytics + Application Insights + security alert rules
param location string
param workspaceName string
param appInsightsName string

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 } // hard cost cap for a demo environment
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
  }
}

// Mirrors mediflow/services/audit/main.py security_alerts(): brute force & break-glass.
resource bruteForce 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${appInsightsName}-brute-force'
  location: location
  properties: {
    displayName: 'MediFlow - repeated failed logins'
    severity: 1
    enabled: true
    scopes: [ appInsights.id ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'traces | where customDimensions.action == "auth.login" and customDimensions.outcome == "failure" | summarize n = count() by actor = tostring(customDimensions.actor)'
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'n'
          dimensions: [ { name: 'actor', operator: 'Include', values: [ '*' ] } ]
          operator: 'GreaterThanOrEqual'
          threshold: 5
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
  }
}

resource breakGlass 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${appInsightsName}-break-glass'
  location: location
  properties: {
    displayName: 'MediFlow - emergency (break-glass) record access'
    severity: 0
    enabled: true
    scopes: [ appInsights.id ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          query: 'traces | where customDimensions.action == "patient.breakglass"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
  }
}

output workspaceId string = workspace.id
output workspaceCustomerId string = workspace.properties.customerId
#disable-next-line outputs-should-not-contain-secrets
output workspaceKey string = workspace.listKeys().primarySharedKey
output appInsightsConnectionString string = appInsights.properties.ConnectionString
