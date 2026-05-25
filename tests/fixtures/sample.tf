# Sample Terraform configuration exercising the HCL parser.
#
# Covers: resources, data sources, modules, variables, outputs, locals,
# providers, the terraform block, cross-resource references, variable
# references, local references, data source references, depends_on,
# lifecycle blocks, template interpolations, function call arguments,
# built-in namespace objects, and dynamic blocks.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "t2.micro"
}

locals {
  name_prefix = "myapp"
  full_name   = "${local.name_prefix}-web"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  tags = {
    Name = local.full_name
  }
}

resource "aws_instance" "web" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  subnet_id     = aws_subnet.main.id

  tags = {
    Name = local.full_name
  }

  depends_on = [aws_vpc.main]
}

resource "aws_subnet" "main" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
}

data "aws_ami" "ubuntu" {
  most_recent = true

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*"]
  }

  owners = ["099720109477"]
}

module "security" {
  source = "./modules/security"

  vpc_id      = aws_vpc.main.id
  environment = "production"
}

output "instance_ip" {
  value       = aws_instance.web.public_ip
  description = "The public IP of the web instance"
}

output "vpc_id" {
  value = aws_vpc.main.id
}

# ---------------------------------------------------------------------------
# Variable reference inside a function call argument (count = length(var.x))
# and inside an index expression (var.x[count.index]).  count.index is a
# block-local meta-argument and must not produce a REFERENCES edge.
# ---------------------------------------------------------------------------
variable "subnet_ids" {
  type = list(string)
}

resource "aws_instance" "fleet" {
  count         = length(var.subnet_ids)
  subnet_id     = var.subnet_ids[count.index]
  instance_type = var.instance_type

  tags = {
    Name = "fleet-${count.index}"
  }
}

# ---------------------------------------------------------------------------
# Resource-to-resource for_each chaining.  The 'each' iterator is block-local
# and must not produce a REFERENCES edge.
# ---------------------------------------------------------------------------
resource "aws_internet_gateway" "gw" {
  for_each = aws_vpc.main
  vpc_id   = each.value.id
}

# ---------------------------------------------------------------------------
# Variable reference inside a template string interpolation ("${var.x}").
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "static" {
  bucket = "${var.region}-static-assets"
}

# ---------------------------------------------------------------------------
# Terraform built-in namespace objects (path.module, terraform.workspace)
# are not resource references and must not produce REFERENCES edges.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "tfstate" {
  bucket = "tfstate-${terraform.workspace}"

  tags = {
    Module = path.module
  }
}

# ---------------------------------------------------------------------------
# Reference inside a lifecycle nested block (replace_triggered_by).
# ---------------------------------------------------------------------------
resource "aws_autoscaling_group" "web" {
  min_size = 1
  max_size = 3

  lifecycle {
    replace_triggered_by = [aws_launch_template.web.id]
  }
}

# ---------------------------------------------------------------------------
# Dynamic block with default iterator name ('ingress' = block label).
# The for_each variable reference must be extracted; references to
# ingress.value.* inside the content block must not produce edges.
# ---------------------------------------------------------------------------
variable "ingress_rules" {
  type = list(object({
    from_port = number
    to_port   = number
    protocol  = string
  }))
}

resource "aws_security_group" "main" {
  vpc_id = aws_vpc.main.id

  dynamic "ingress" {
    for_each = var.ingress_rules
    content {
      from_port = ingress.value.from_port
      to_port   = ingress.value.to_port
      protocol  = ingress.value.protocol
    }
  }
}

# ---------------------------------------------------------------------------
# Dynamic block with default iterator name ('setting' = block label).
# References to setting.value[...] inside the content block must not
# produce REFERENCES edges; var.settings and the resource reference on
# 'application' must be extracted.
# ---------------------------------------------------------------------------
variable "settings" {
  type = list(object({
    namespace = string
    name      = string
    value     = string
  }))
}

resource "aws_elastic_beanstalk_environment" "tfenvtest" {
  name        = "tf-test-name"
  application = aws_elastic_beanstalk_application.tftest.name

  dynamic "setting" {
    for_each = var.settings
    content {
      namespace = setting.value["namespace"]
      name      = setting.value["name"]
      value     = setting.value["value"]
    }
  }
}

# ---------------------------------------------------------------------------
# Dynamic block with a custom iterator name set via the 'iterator' argument
# ('srv' overrides the default 'condition' label).  References to
# srv.value[...] inside the content block must not produce REFERENCES edges;
# var.server_list must be extracted.
# ---------------------------------------------------------------------------
variable "server_list" {
  type = list(object({
    port     = number
    protocol = string
  }))
}

resource "aws_lb_listener_rule" "hosts" {
  dynamic "condition" {
    for_each = var.server_list
    iterator = srv
    content {
      host_header {
        values = [srv.value["port"]]
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Multi-level nested dynamic blocks.  Each level introduces its own iterator
# symbol (origin_group, origin).  Only var.load_balancer_origin_groups must
# produce a REFERENCES edge; all iterator references (origin_group.key,
# origin_group.value.origins, origin.value.hostname) must be suppressed.
# ---------------------------------------------------------------------------
variable "load_balancer_origin_groups" {
  type = map(object({
    origins = set(object({
      hostname = string
    }))
  }))
}

resource "aws_cloudfront_distribution" "cdn" {
  dynamic "origin_group" {
    for_each = var.load_balancer_origin_groups
    content {
      name = origin_group.key

      dynamic "origin" {
        for_each = origin_group.value.origins
        content {
          hostname = origin.value.hostname
        }
      }
    }
  }
}
