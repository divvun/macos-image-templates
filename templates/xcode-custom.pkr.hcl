packer {
  required_plugins {
    tart = {
      version = ">= 1.12.0"
      source  = "github.com/cirruslabs/tart"
    }
  }
}

variable "macos_version" {
  type = string
}

variable "xcode_version" {
  type = list(string)
}

variable "additional_ios_builds" {
  type = list(string)
  default = []
}

variable "xcode_components" {
  type    = list(string)
  default = []
  description = "Additional Xcode components to download."
}

variable "expected_runtimes_file" {
  type    = string
  default = ""
  description = "Path to file containing expected simulator runtimes. If empty, runtime verification is skipped."
}

variable "tag" {
  type = string
  default = ""
}

variable "disk_size" {
  type = number
  default = 120
}

variable "disk_free_mb" {
  type = number
  default = 15000
}

source "tart-cli" "tart" {
  vm_base_name = "ghcr.io/cirruslabs/macos-${var.macos_version}-base-custom:latest"
  // use tag or the first element of the xcode_version list
  vm_name      = "${var.macos_version}-xcode-custom:${var.tag != "" ? var.tag : var.xcode_version[0]}"
  cpu_count    = 4
  memory_gb    = 8
  disk_size_gb = var.disk_size
  headless     = true
  ssh_password = "admin"
  ssh_username = "admin"
  ssh_timeout  = "120s"
}

locals {
  xcode_install_provisioners = [
    for version in reverse(sort(var.xcode_version)) : {
      type = "shell"
      inline = [
        "source ~/.zprofile",
        "sudo xcodes install ${version} --experimental-unxip --path /Users/admin/Downloads/Xcode_${version}.xip --select --empty-trash",
        // get selected xcode path, strip /Contents/Developer and move to GitHub compatible locations
        "INSTALLED_PATH=$(xcodes select -p)",
        "CONTENTS_DIR=$(dirname $INSTALLED_PATH)",
        "APP_DIR=$(dirname $CONTENTS_DIR)",
        "sudo mv $APP_DIR /Applications/Xcode_${version}.app",
        "sudo xcode-select -s /Applications/Xcode_${version}.app",
        "xcodebuild -downloadPlatform iOS",
        "xcodebuild -runFirstLaunch",
        "df -h",
      ]
    }
  ]
}

build {
  sources = ["source.tart-cli.tart"]

  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew --version",
      "brew update",
      "brew upgrade",
    ]
  }

  # Install xcodes tool for Xcode management
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew install xcodesorg/made/xcodes",
      "xcodes version",
    ]
  }

  # Copy Xcode installers from local cache
  provisioner "file" {
    sources      = [ for version in var.xcode_version : pathexpand("~/XcodesCache/Xcode_${version}.xip")]
    destination = "/Users/admin/Downloads/"
  }

  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "df -h",
    ]
  }

  # Install all Xcode versions
  dynamic "provisioner" {
    for_each = local.xcode_install_provisioners
    labels = ["shell"]
    content {
      inline = provisioner.value.inline
    }
  }

  # Download platforms for multiple Xcode versions if available
  dynamic "provisioner" {
    for_each = length(var.xcode_version) > 2 ? [2] : []
    labels = ["shell"]
    content {
      inline = [
        "source ~/.zprofile",
        "sudo xcodes select '${var.xcode_version[2]}'",
        "xcodebuild -downloadAllPlatforms",
      ]
    }
  }

  dynamic "provisioner" {
    for_each = length(var.xcode_version) > 1 ? [1] : []
    labels = ["shell"]
    content {
      inline = [
        "source ~/.zprofile",
        "sudo xcodes select '${var.xcode_version[1]}'",
        "xcodebuild -downloadAllPlatforms",
      ]
    }
  }

  # Set primary Xcode version and download platforms
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "sudo xcodes select '${var.xcode_version[0]}'",
      "xcodebuild -downloadAllPlatforms",
    ]
  }

  # Download additional iOS build versions if specified
  provisioner "shell" {
    inline = concat(
      ["source ~/.zprofile"],
      [
        for runtime in var.additional_ios_builds : "xcodebuild -downloadPlatform iOS -buildVersion ${runtime}"
      ]
    )
  }

  # Download additional Xcode components if specified
  provisioner "shell" {
    inline = concat(
      ["source ~/.zprofile"],
      [
        for component in var.xcode_components : "xcodebuild -downloadComponent ${component}"
      ]
    )
  }

  # Install essential iOS development tools (minimal set)
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew install xcbeautify swiftformat swiftlint swiftgen"
    ]
  }

  # Copy expected runtimes file if provided
  dynamic "provisioner" {
    for_each = var.expected_runtimes_file != "" ? [1] : []
    labels = ["file"]
    content {
      source      = var.expected_runtimes_file
      destination = "/Users/admin/runtimes.expected.txt"
    }
  }

  # Verify simulator runtimes match expected list if file was provided
  dynamic "provisioner" {
    for_each = var.expected_runtimes_file != "" ? [1] : []
    labels = ["shell"]
    content {
      inline = [
        "source ~/.zprofile",
        "xcrun simctl list runtimes > /Users/admin/runtimes.actual.txt",
        "diff -q /Users/admin/runtimes.actual.txt /Users/admin/runtimes.expected.txt || (echo 'Simulator runtimes do not match expected list' && cat /Users/admin/runtimes.actual.txt && exit 1)",
        "rm /Users/admin/runtimes.actual.txt /Users/admin/runtimes.expected.txt"
      ]
    }
  }

  # Check there is at least the required amount of free space
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "df -h",
      "FREE_MB=$(df -m | awk '{print $4}' | head -n 2 | tail -n 1)",
      "[[ $FREE_MB -gt ${var.disk_free_mb} ]] && echo \"OK - $${FREE_MB}MB free\" || (echo \"ERROR: Only $${FREE_MB}MB free, need ${var.disk_free_mb}MB\" && exit 1)"
    ]
  }

  # Disable apsd daemon as it causes high CPU usage after boot
  provisioner "shell" {
    inline = [
      "sudo launchctl unload -w /System/Library/LaunchDaemons/com.apple.apsd.plist"
    ]
  }

  # Wait for the "update_dyld_sim_shared_cache" process to finish
  # to avoid wasting CPU cycles after boot
  provisioner "shell" {
    inline = [
      "echo 'Waiting for dyld cache update to complete...'",
      "sleep 1800"
    ]
  }

  # Final health checks
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "xcodes installed",
      "xcodebuild -version",
      "swift --version",
      "df -h"
    ]
  }
}