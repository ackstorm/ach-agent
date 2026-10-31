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
{{- if and (eq .Values.storage.remote.type "gcs") (not .Values.storage.remote.gcs.bucket) -}}
{{- fail "storage.remote.gcs.bucket is required with storage.remote.type=gcs" -}}
{{- end -}}
{{- if and (eq .Values.storage.remote.type "gcs") (not .Values.storage.enabled) -}}
{{- fail "storage.remote.type=gcs needs storage.enabled: control pods reach GCS only through the storage server" -}}
{{- end -}}
{{- if eq (include "ach-agent.storage.active" .) "true" -}}
{{- if not .Values.storage.namespaces -}}
{{- fail "storage.namespaces must list the Agent namespaces that get a storage server (or set storage.enabled=false)" -}}
{{- end -}}
{{- if eq .Values.storage.remote.type "gcs" -}}
{{- if not .Values.storage.remote.gcs.project -}}
{{- fail "storage.remote.gcs.project is required with storage.remote.type=gcs" -}}
{{- end -}}
{{- else if not .Values.runtime.storage.s3.region -}}
{{- fail "runtime.storage.s3.region is required when the storage server is enabled" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The bucket the storage server replicates to: the GCS bucket, or the S3 bucket control pods use without it. */}}
{{- define "ach-agent.storage.bucket" -}}
{{- if eq .Values.storage.remote.type "gcs" }}{{ .Values.storage.remote.gcs.bucket }}{{ else }}{{ .Values.runtime.storage.s3.bucket }}{{ end -}}
{{- end -}}

{{/* The storage server runs when it is enabled and there is a bucket to replicate to. */}}
{{- define "ach-agent.storage.active" -}}
{{- if and .Values.storage.enabled (include "ach-agent.storage.bucket" .) }}true{{ end -}}
{{- end -}}

{{- define "ach-agent.storage.selectorLabels" -}}
app.kubernetes.io/name: ach-agent-storage
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ach-agent.storage.labels" -}}
{{ include "ach-agent.storage.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
