terraform {
  backend "s3" {
    bucket = "example-tfstate-123"
    key    = "edge.tfstate"
  }
}
