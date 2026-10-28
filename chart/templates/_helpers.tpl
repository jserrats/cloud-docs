{{- define "cloud-docs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "cloud-docs.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "cloud-docs.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "cloud-docs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cloud-docs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "cloud-docs.labels" -}}
{{ include "cloud-docs.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "cloud-docs.sshSecret" -}}
{{- default (include "cloud-docs.fullname" .) .Values.sshKeySecret.name }}
{{- end }}
