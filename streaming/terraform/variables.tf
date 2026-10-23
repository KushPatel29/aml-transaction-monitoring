variable "kubeconfig" {
  description = "Path to the kubeconfig that reaches the cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "Context to deploy into. CI creates a kind cluster named aml."
  type        = string
  default     = "kind-aml"
}

variable "namespace" {
  description = "Namespace for the broker, the monitor and its jobs."
  type        = string
  default     = "aml-stream"
}

variable "image" {
  description = "The streaming image (streaming/Dockerfile), already loaded into the cluster."
  type        = string
  default     = "aml-stream:ci"
}

variable "redpanda_image" {
  description = "Kafka-compatible broker, one node in dev-container mode."
  type        = string
  default     = "docker.redpanda.com/redpandadata/redpanda:v24.2.7"
}

variable "monitor_replicas" {
  description = "Monitor pods in the consumer group. Two, so one can be killed mid-stream."
  type        = number
  default     = 2

  validation {
    condition     = var.monitor_replicas >= 2 && var.monitor_replicas <= 6
    error_message = "Between 2 (one survives a kill) and 6 (one per partition)."
  }
}

variable "produce_rate" {
  description = "Messages per second the producer replays. Slow enough that a pod can be killed mid-stream."
  type        = number
  default     = 4000
}
