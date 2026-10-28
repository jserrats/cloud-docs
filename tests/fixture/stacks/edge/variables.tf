variable "domain" {
  type = string
}

variable "tunnels" {
  type = map(object({
    zone    = optional(string)
    ingress = list(object({ hostname = string, service = string }))
  }))
}

variable "env_name" {
  type    = string
  default = "deploy"
}
