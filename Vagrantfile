# -*- mode: ruby -*-
# vi: set ft=ruby :
#
# One container for the whole lab: cmunet emulates the network on localhost,
# so no second machine is needed. Same Ubuntu image as the checkpoints, so your
# code builds and runs here the way it does on Gradescope.
#
#   CMU_TCP=../cmu-tcp vagrant up --provider=docker
#   vagrant ssh
#
# This repository is mounted at /lab (the same folder as cmu-tcp-lab on your
# machine). You copy your Checkpoint 2 project into it as /lab/baseline and
# work there. Your Checkpoint 2 repository (CMU_TCP, default ../cmu-tcp) is
# mounted at /cmu-tcp only so you can copy it.

cmu_tcp = File.expand_path(ENV.fetch("CMU_TCP", "../cmu-tcp"), __dir__)

Vagrant.configure(2) do |config|
  config.vm.synced_folder ".", "/lab"
  config.vm.synced_folder cmu_tcp, "/cmu-tcp" if File.directory?(cmu_tcp)
  config.vm.provision "shell", inline: "sudo /lab/setup/setup.sh"
  config.vm.provision "shell", inline: "echo 'cd /lab' >> /home/vagrant/.bashrc"
  config.ssh.insert_key = false

  config.vm.provider "docker" do |docker, override|
    override.vm.box = nil
    docker.image = "rofrano/vagrant-provider:ubuntu-22.04"
    docker.remains_running = true
    docker.has_ssh = true
    docker.privileged = true
    # A --mount (not docker.volumes) keeps Windows from rewriting the
    # /sys/fs/cgroup path, which made the container exit immediately.
    docker.create_args = [
      "--cgroupns=host",
      "--mount", "type=bind,source=/sys/fs/cgroup,target=/sys/fs/cgroup"
    ]
  end

  config.vm.provider "virtualbox" do |v, override|
    override.vm.box = "ubuntu/jammy64"
  end

  config.vm.define :lab, primary: true do |host|
    host.vm.hostname = "lab"
  end
end
