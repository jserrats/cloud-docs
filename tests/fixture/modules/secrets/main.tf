variable "path_prefix" {
  type = string
}

variable "env_name" {
  type = string
}

output "names" {
  value = ["${var.path_prefix}/token"]
}

output "subjects" {
  value = ["repo:o/r:environment:${var.env_name}"]
}
