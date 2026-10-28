locals {
  tunnels = { for k, t in var.tunnels : k => merge(t, { zone = t.zone == null ? var.domain : t.zone }) }
  records = merge([for k, t in local.tunnels : { for i in t.ingress : "${k}/${i.hostname}" => { tunnel = k, fqdn = "${i.hostname}.${t.zone}" } }]...)
}

# ---------------------------------------------------------------------------
# Tunnels
#
# config_src = "cloudflare" is prose, not code.
# ---------------------------------------------------------------------------

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  for_each = local.tunnels
  name     = each.key
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  for_each  = local.tunnels
  tunnel_id = cloudflare_zero_trust_tunnel_cloudflared.this[each.key].id
  config = {
    ingress = [for i in each.value.ingress : { hostname = i.hostname, service = i.service }]
  }
}

# One record per tunnel hostname.
resource "cloudflare_dns_record" "tunnel" {
  for_each = local.records
  name     = each.value.fqdn
  type     = "CNAME"
  content  = "${cloudflare_zero_trust_tunnel_cloudflared.this[each.value.tunnel].id}.cfargotunnel.com"
}

module "secrets" {
  source      = "../../modules/secrets"
  path_prefix = "/edge"
  env_name    = var.env_name
}

resource "aws_s3_bucket" "state" {
  bucket = "example-tfstate-${data.aws_caller_identity.current.account_id}"
}

# Versioning, folded into the bucket.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
}
