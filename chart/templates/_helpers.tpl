{{/* One operator per cluster: the names are fixed because the mesh policy names the ServiceAccount. */}}
{{- define "ach-agent.fullname" -}}ach-agent-operator{{- end -}}
{{- define "ach-agent.serviceAccountName" -}}ach-agent-operator{{- end -}}

{{- define "ach-agent.selectorLabels" -}}
app.kubernetes.io/name: ach-agent-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ach-agent.labels" -}}
{{ include "ach-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ach-agent.validate" -}}
{{- if not .Values.openshell.gateway -}}
{{- fail "openshell.gateway is required (the OpenShell gateway URL, e.g. http://openshell.openshell.svc.cluster.local:8080)" -}}
{{- end -}}
{{- if ne .Values.openshell.mesh "istio-ambient" -}}
{{- fail "openshell.mesh must be istio-ambient: ach-agent requires an Istio ambient mesh to protect the OpenShell gateway" -}}
{{- end -}}
{{- end -}}
