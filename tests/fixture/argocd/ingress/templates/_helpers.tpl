{{- define "ingress.hosts" -}}
{{- $values := (index . 0).Values -}}
{{- $name := index . 1 -}}
Host(`{{ $name }}.{{ required "domain" $values.domain }}`) || Host(`{{ $name }}.{{ required "rootDomain" $values.rootDomain }}`)
{{- end -}}
