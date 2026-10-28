domain = "example.com"

tunnels = {
  # The tunnel for the lab.
  lab = {
    ingress = [
      { hostname = "app", service = "http://box.lan:8123" },
    ]
  }
}
