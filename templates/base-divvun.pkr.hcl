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

source "tart-cli" "tart" {
  vm_base_name = "ghcr.io/cirruslabs/macos-${var.macos_version}-vanilla:latest"
  vm_name      = "${var.macos_version}-base-divvun"
  cpu_count    = 4
  memory_gb    = 8
  disk_size_gb = 50
  ssh_password = "admin"
  ssh_username = "admin"
  ssh_timeout  = "120s"
}

build {
  sources = ["source.tart-cli.tart"]

  # Set dynamic hostname
  provisioner "shell" {
    inline = [
      "NEW_HOSTNAME=\"vm-builder-$(uuidgen | cut -c1-5)\"",
      "echo \"Setting hostname to $NEW_HOSTNAME...\"",
      "sudo scutil --set HostName \"$NEW_HOSTNAME\"",
      "sudo scutil --set LocalHostName \"$NEW_HOSTNAME\"",
      "sudo scutil --set ComputerName \"$NEW_HOSTNAME\""
    ]
  }

  provisioner "file" {
    source      = "data/limit.maxfiles.plist"
    destination = "~/limit.maxfiles.plist"
  }

  provisioner "shell" {
    inline = [
      "echo 'Configuring maxfiles...'",
      "sudo mv ~/limit.maxfiles.plist /Library/LaunchDaemons/limit.maxfiles.plist",
      "sudo chown root:wheel /Library/LaunchDaemons/limit.maxfiles.plist",
      "sudo chmod 0644 /Library/LaunchDaemons/limit.maxfiles.plist",
      "echo 'Disabling spotlight...'",
      "sudo mdutil -i off / || true",
      "sudo mdutil -i off /System/Volumes/Data || true",
    ]
  }

  # Create a symlink for bash compatibility
  provisioner "shell" {
    inline = [
      "touch ~/.zprofile",
      "ln -s ~/.zprofile ~/.profile",
    ]
  }

  # Install Homebrew
  provisioner "shell" {
    inline = [
      "/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"",
      "echo \"export LANG=en_US.UTF-8\" >> ~/.zprofile",
      "echo 'eval \"$(/opt/homebrew/bin/brew shellenv)\"' >> ~/.zprofile",
      "echo \"export HOMEBREW_NO_AUTO_UPDATE=1\" >> ~/.zprofile",
      "echo \"export HOMEBREW_NO_INSTALL_CLEANUP=1\" >> ~/.zprofile",
    ]
  }

  # Enable Rosetta
  provisioner "shell" {
    inline = [
      "sudo softwareupdate --install-rosetta --agree-to-license"
    ]
  }

  # Install base development tools (removed gitlab-runner)
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew --version",
      "brew update",
      "brew install wget unzip zip ca-certificates cmake git-lfs jq yq gh",
      "brew install curl || true", // doesn't work on Monterey
      "brew install --cask git-credential-manager",
      "git lfs install"
    ]
  }

  # Install custom requirements
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew install cocoapods just bison flex python@3.11 boost pytorch",
      "brew link bison --force",
      "brew link flex --force"
    ]
  }

  # Configure SSH with ed25519 key generation
  provisioner "shell" {
    inline = [
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
      "ssh-keygen -t ed25519 -N '' -f $HOME/.ssh/id_ed25519"
    ]
  }

  # Add GitHub to known hosts
  provisioner "file" {
    source      = "data/github_known_hosts"
    destination = "~/.ssh/known_hosts"
  }

  # Install Tailscale
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "echo 'Installing Tailscale CLI + daemon...'",
      "brew install tailscale",
      "sudo tailscaled install-system-daemon"
    ]
  }

  # Install Rust with iOS targets
  provisioner "shell" {
    inline = [
      # Install Rust with default target
      "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y",
      "echo 'source ~/.cargo/env' >> ~/.zprofile",
      "source ~/.cargo/env",
      # Add all required targets for iOS and macOS development
      "rustup target add x86_64-apple-darwin",      # macOS Intel
      "rustup target add aarch64-apple-darwin",      # macOS Apple Silicon (default, but explicit)
      "rustup target add x86_64-apple-ios",          # iOS Intel simulator
      "rustup target add aarch64-apple-ios",         # iOS device
      "rustup target add aarch64-apple-ios-sim"      # iOS Apple Silicon simulator
    ]
  }

  # Install Buildkite agent (without token configuration)
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew install buildkite/buildkite/buildkite-agent"
    ]
  }

  # Install Deno
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew install deno",
      "deno --version"
    ]
  }

  # Install fastlane
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew install fastlane",
      "fastlane --version"
    ]
  }

  # Enable Safari driver
  provisioner "shell" {
    inline = [
      "sudo safaridriver --enable",
    ]
  }

  # Enable UI automation
  provisioner "shell" {
    script = "scripts/automationmodetool.expect"
  }

  # Health checks (removed /Users/runner test since we don't create it)
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "test -f ~/.ssh/known_hosts",
      "test -f ~/.ssh/id_ed25519",
      "test -f ~/.ssh/id_ed25519.pub"
    ]
  }

  # Guest agent for Tart VMs
  provisioner "file" {
    source      = "data/tart-guest-daemon.plist"
    destination = "~/tart-guest-daemon.plist"
  }
  
  provisioner "file" {
    source      = "data/tart-guest-agent.plist"
    destination = "~/tart-guest-agent.plist"
  }

  provisioner "shell" {
    inline = [
      # Install Tart Guest Agent
      "source ~/.zprofile",
      "brew install cirruslabs/cli/tart-guest-agent",

      # Install daemon variant of the Tart Guest Agent
      "sudo mv ~/tart-guest-daemon.plist /Library/LaunchDaemons/org.cirruslabs.tart-guest-daemon.plist",
      "sudo chown root:wheel /Library/LaunchDaemons/org.cirruslabs.tart-guest-daemon.plist",
      "sudo chmod 0644 /Library/LaunchDaemons/org.cirruslabs.tart-guest-daemon.plist",

      # Install agent variant of the Tart Guest Agent
      "sudo mv ~/tart-guest-agent.plist /Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist",
      "sudo chown root:wheel /Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist",
      "sudo chmod 0644 /Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist",
    ]
  }

  # Create persistent storage symlink
  provisioner "shell" {
    inline = [
      "sudo ln -s '/Volumes/My Shared Files/persistent' /opt/d"
    ]
  }

  # Final upgrade of all packages
  provisioner "shell" {
    inline = [
      "source ~/.zprofile",
      "brew upgrade"
    ]
  }
}