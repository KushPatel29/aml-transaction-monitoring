# The real-time monitor on Kubernetes: a one-node Redpanda broker, the monitor
# as a two-replica consumer group, and three jobs that run in order (create
# topics, replay a year of transactions, verify the stream against the batch).
# `terraform apply` waits on each job, so it succeeds only if the verifier
# finds the stream's alerts identical to the batch rules'.

locals {
  bootstrap = "redpanda.${var.namespace}.svc.cluster.local:9092"
  part_of   = { "app.kubernetes.io/part-of" = "aml-stream" }

  # The monitor and its jobs write nothing but a health file to /tmp.
  locked_down = {
    run_as_non_root            = true
    run_as_user                = 10001
    read_only_root_filesystem  = true
    allow_privilege_escalation = false
  }
}

resource "kubernetes_namespace_v1" "aml" {
  metadata {
    name   = var.namespace
    labels = local.part_of
  }
}

# ----------------------------------------------------------------- the broker
resource "kubernetes_deployment_v1" "redpanda" {
  metadata {
    name      = "redpanda"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = merge(local.part_of, { app = "redpanda" })
  }

  spec {
    replicas = 1

    selector {
      match_labels = { app = "redpanda" }
    }

    template {
      metadata {
        labels = merge(local.part_of, { app = "redpanda" })
      }

      spec {
        container {
          name              = "redpanda"
          image             = var.redpanda_image
          image_pull_policy = "IfNotPresent"
          args = [
            "redpanda", "start",
            "--mode", "dev-container",
            "--smp", "1",
            "--memory", "1G",
            "--kafka-addr", "internal://0.0.0.0:9092",
            "--advertise-kafka-addr", "internal://${local.bootstrap}",
          ]

          port {
            name           = "kafka"
            container_port = 9092
          }

          readiness_probe {
            tcp_socket {
              port = 9092
            }
            initial_delay_seconds = 5
            period_seconds        = 3
          }

          resources {
            requests = { cpu = "500m", memory = "1Gi" }
            limits   = { memory = "2Gi" }
          }

          security_context {
            allow_privilege_escalation = false
          }
        }
      }
    }
  }

  wait_for_rollout = true
}

resource "kubernetes_service_v1" "redpanda" {
  metadata {
    name      = "redpanda"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = local.part_of
  }

  spec {
    selector = { app = "redpanda" }

    port {
      name        = "kafka"
      port        = 9092
      target_port = 9092
    }
  }
}

# ------------------------------------------------------------------- topics
resource "kubernetes_job_v1" "topics" {
  metadata {
    name      = "topics"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = local.part_of
  }

  spec {
    backoff_limit = 6

    template {
      metadata {
        labels = merge(local.part_of, { app = "topics" })
      }

      spec {
        restart_policy = "Never"

        container {
          name              = "topics"
          image             = var.image
          image_pull_policy = "Never"
          args              = ["topics"]

          env {
            name  = "KAFKA_BOOTSTRAP"
            value = local.bootstrap
          }

          security_context {
            run_as_non_root            = local.locked_down.run_as_non_root
            run_as_user                = local.locked_down.run_as_user
            read_only_root_filesystem  = local.locked_down.read_only_root_filesystem
            allow_privilege_escalation = local.locked_down.allow_privilege_escalation
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
  }

  depends_on = [kubernetes_deployment_v1.redpanda, kubernetes_service_v1.redpanda]
}

# -------------------------------------------------------------- the monitor
resource "kubernetes_deployment_v1" "monitor" {
  metadata {
    name      = "monitor"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = merge(local.part_of, { app = "monitor" })
  }

  spec {
    replicas = var.monitor_replicas

    selector {
      match_labels = { app = "monitor" }
    }

    template {
      metadata {
        labels = merge(local.part_of, { app = "monitor" })
      }

      spec {
        termination_grace_period_seconds = 20

        container {
          name              = "monitor"
          image             = var.image
          image_pull_policy = "Never"
          args              = ["process"]

          env {
            name  = "KAFKA_BOOTSTRAP"
            value = local.bootstrap
          }

          # Ready once it has subscribed; alive while the poll loop turns.
          readiness_probe {
            exec {
              command = ["test", "-f", "/tmp/healthy"]
            }
            period_seconds = 2
          }

          liveness_probe {
            exec {
              command = ["sh", "-c", "test $(( $(date +%s) - $(stat -c %Y /tmp/healthy) )) -lt 30"]
            }
            initial_delay_seconds = 20
            period_seconds        = 10
          }

          resources {
            requests = { cpu = "250m", memory = "256Mi" }
            limits   = { memory = "512Mi" }
          }

          security_context {
            run_as_non_root            = local.locked_down.run_as_non_root
            run_as_user                = local.locked_down.run_as_user
            read_only_root_filesystem  = local.locked_down.read_only_root_filesystem
            allow_privilege_escalation = local.locked_down.allow_privilege_escalation
          }

          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }

        volume {
          name = "tmp"
          empty_dir {}
        }
      }
    }
  }

  wait_for_rollout = true

  depends_on = [kubernetes_job_v1.topics]
}

resource "kubernetes_pod_disruption_budget_v1" "monitor" {
  metadata {
    name      = "monitor"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = local.part_of
  }

  spec {
    min_available = 1

    selector {
      match_labels = { app = "monitor" }
    }
  }
}

# ------------------------------------------------- replay, then verify
resource "kubernetes_job_v1" "producer" {
  metadata {
    name      = "producer"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = local.part_of
  }

  spec {
    backoff_limit = 0

    template {
      metadata {
        labels = merge(local.part_of, { app = "producer" })
      }

      spec {
        restart_policy = "Never"

        container {
          name              = "producer"
          image             = var.image
          image_pull_policy = "Never"
          args              = ["produce"]

          env {
            name  = "KAFKA_BOOTSTRAP"
            value = local.bootstrap
          }

          env {
            name  = "PRODUCE_RATE"
            value = tostring(var.produce_rate)
          }

          security_context {
            run_as_non_root            = local.locked_down.run_as_non_root
            run_as_user                = local.locked_down.run_as_user
            read_only_root_filesystem  = local.locked_down.read_only_root_filesystem
            allow_privilege_escalation = local.locked_down.allow_privilege_escalation
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "10m"
  }

  depends_on = [kubernetes_deployment_v1.monitor]
}

resource "kubernetes_job_v1" "verifier" {
  metadata {
    name      = "verifier"
    namespace = kubernetes_namespace_v1.aml.metadata[0].name
    labels    = local.part_of
  }

  spec {
    backoff_limit = 0

    template {
      metadata {
        labels = merge(local.part_of, { app = "verifier" })
      }

      spec {
        restart_policy = "Never"

        container {
          name              = "verifier"
          image             = var.image
          image_pull_policy = "Never"
          args              = ["verify"]

          env {
            name  = "KAFKA_BOOTSTRAP"
            value = local.bootstrap
          }

          security_context {
            run_as_non_root            = local.locked_down.run_as_non_root
            run_as_user                = local.locked_down.run_as_user
            read_only_root_filesystem  = local.locked_down.read_only_root_filesystem
            allow_privilege_escalation = local.locked_down.allow_privilege_escalation
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "15m"
  }

  depends_on = [kubernetes_job_v1.producer]
}
