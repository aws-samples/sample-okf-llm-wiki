# Minimal VPC for the harvest runtime. S3 Files mounts REQUIRE VPC networking
# (NFSv4.2/TLS on TCP 2049), and the harvest container also needs egress to
# Bedrock / Glue / Athena / S3 Vectors. We create a small VPC with private
# subnets + a NAT gateway ONLY when the user hasn't supplied their own subnets
# (var.harvest_vpc_subnet_ids empty). If you bring your own subnets/SGs, this is
# skipped entirely.

locals {
  create_vpc = length(var.harvest_vpc_subnet_ids) == 0

  # How many subnets the mount targets span (2 in the auto-VPC case).
  subnet_count = local.create_vpc ? 2 : length(var.harvest_vpc_subnet_ids)

  # The subnets/SGs the rest of the stack (s3files mount target, runtime VPC
  # config) actually uses: the created ones, or the user-provided ones.
  effective_subnet_ids = local.create_vpc ? [for s in aws_subnet.private : s.id] : var.harvest_vpc_subnet_ids
  effective_sg_ids     = local.create_vpc ? [aws_security_group.harvest[0].id] : var.harvest_vpc_security_group_ids

  # A map keyed by a STATIC index (known at plan time) -> the subnet id (which
  # may be an apply-time value from the auto-created VPC). for_each needs keys
  # known at plan; keying on the index avoids the "values derived from resource
  # attributes" error while still creating one mount target per subnet.
  mount_target_subnets = local.s3files_enabled ? {
    for idx in range(local.subnet_count) : tostring(idx) => local.effective_subnet_ids[idx]
  } : {}
}

data "aws_availability_zones" "available" {
  count = local.create_vpc ? 1 : 0
  state = "available"
}

resource "aws_vpc" "harvest" {
  count                = local.create_vpc ? 1 : 0
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = "${var.name_prefix}-harvest" })
}

resource "aws_internet_gateway" "harvest" {
  count  = local.create_vpc ? 1 : 0
  vpc_id = aws_vpc.harvest[0].id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-harvest" })
}

# Two public subnets (for the NAT gateway) + two private subnets (the runtime).
# map_public_ip_on_launch stays FALSE (CKV_AWS_130): the ONLY thing that lives in
# these public subnets is the NAT gateway, which reaches the internet via its own
# attached EIP (aws_eip.nat) + the IGW route — not via subnet auto-assign. Nothing
# is ever launched here that would need an auto-assigned public IP, so leaving it
# off closes the "any future ENI silently gets a public IP" hole at no cost.
resource "aws_subnet" "public" {
  count                   = local.create_vpc ? 2 : 0
  vpc_id                  = aws_vpc.harvest[0].id
  cidr_block              = "10.42.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.available[0].names[count.index]
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "${var.name_prefix}-harvest-public-${count.index}" })
}

resource "aws_subnet" "private" {
  count             = local.create_vpc ? 2 : 0
  vpc_id            = aws_vpc.harvest[0].id
  cidr_block        = "10.42.${count.index + 10}.0/24"
  availability_zone = data.aws_availability_zones.available[0].names[count.index]
  tags              = merge(var.tags, { Name = "${var.name_prefix}-harvest-private-${count.index}" })
}

resource "aws_eip" "nat" {
  count      = local.create_vpc ? 1 : 0
  domain     = "vpc"
  tags       = var.tags
  depends_on = [aws_internet_gateway.harvest]
}

resource "aws_nat_gateway" "harvest" {
  count         = local.create_vpc ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = merge(var.tags, { Name = "${var.name_prefix}-harvest" })
  depends_on    = [aws_internet_gateway.harvest]
}

resource "aws_route_table" "public" {
  count  = local.create_vpc ? 1 : 0
  vpc_id = aws_vpc.harvest[0].id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.harvest[0].id
  }
  tags = merge(var.tags, { Name = "${var.name_prefix}-harvest-public" })
}

resource "aws_route_table_association" "public" {
  count          = local.create_vpc ? 2 : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table" "private" {
  count  = local.create_vpc ? 1 : 0
  vpc_id = aws_vpc.harvest[0].id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.harvest[0].id
  }
  tags = merge(var.tags, { Name = "${var.name_prefix}-harvest-private" })
}

resource "aws_route_table_association" "private" {
  count          = local.create_vpc ? 2 : 0
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# Security group for the runtime: all egress (Bedrock/Glue/Athena/S3 Vectors +
# the S3 Files mount target on TCP 2049), and self-ingress on 2049 for the mount.
resource "aws_security_group" "harvest" {
  count       = local.create_vpc ? 1 : 0
  name        = "${var.name_prefix}-harvest"
  description = "OKF harvest runtime + S3 Files mount"
  vpc_id      = aws_vpc.harvest[0].id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # NFS (S3 Files mount) within the SG.
  ingress {
    from_port = 2049
    to_port   = 2049
    protocol  = "tcp"
    self      = true
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-harvest" })
}
