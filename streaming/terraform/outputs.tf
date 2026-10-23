output "namespace" {
  description = "Where everything runs."
  value       = kubernetes_namespace_v1.aml.metadata[0].name
}

output "bootstrap" {
  description = "In-cluster Kafka bootstrap address."
  value       = local.bootstrap
}
