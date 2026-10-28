# Windows 11 workers

The Windows client is `deployment/windows/dist/MinosClient.exe`. Copy that file to the PC and double-click it. A window opens. Enter the server address, the worker token, and the computer name, then press Start. Leave the window open. Stop disconnects this PC. Close the window to exit. It uses mock execution and scoring version v1. Settings, cache, and work files go under `%LOCALAPPDATA%\MinosClient`. Python does not need to be installed.

No Windows 11 machine was available while this tree was built. Unit tests cover path conversion. `minos-worker doctor` reports `windows_hardware` as not run. Do not treat a green Linux pytest run as a Windows end-to-end result.

## What the worker does on Windows

The agent is Python. It uses `pathlib`, `subprocess` argument lists, and threads. It does not call `fork`, `systemd`, or Unix sockets. `os.kill(pid, SIGTERM)` is how `minos-worker stop` asks the process to exit; on Windows, CPython turns `SIGTERM` into `TerminateProcess`.

GATK and hap.py stay inside Linux containers. Docker Desktop must be in **Linux container** mode with the WSL2 engine. The worker process on the Windows host only talks HTTP to the coordinator and `docker` to the desktop engine.

`shared/docker_paths.py` maps `C:\Program Files\Minos Data\bam` to `/c/Program Files/Minos Data/bam`. UNC paths are rejected. The official template mounts `Path.resolve()` strings itself. Docker Desktop usually accepts a Windows path with spaces; if a mount fails, move the cache under the WSL2 filesystem (`\\wsl$\Ubuntu\...` is still a UNC path from the Windows agent — prefer running the agent inside WSL2 so cache paths are `/mnt/...` or ext4 paths under the distro). WSL2 ext4 is also the faster place for BAM reads. A cache on `C:` crosses the 9p/virtiofs boundary.

## Desktop prerequisites

1. Windows 11 with virtualization enabled (BIOS plus the "Virtual Machine Platform" and "Windows Subsystem for Linux" features).
2. Docker Desktop, WSL2 backend, Linux containers. `docker version` must show a Linux server.
3. Settings → Resources → File sharing includes the drive that holds `CACHE_DIR` and `WORK_DIR`.
4. Python 3.11 on the host (or Ubuntu in WSL2; see below).
5. Pull, once, while the machine has internet:

```text
docker pull broadinstitute/gatk:4.5.0.0
docker pull genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2
docker pull quay.io/biocontainers/bcftools:1.20--h8b25389_0
```

The image id from `docker image inspect --format "{{.Id}}" broadinstitute/gatk:4.5.0.0` must equal the id the Ubuntu coordinator recorded when the study was created.

## Optional WSL2 Ubuntu worker

Use this when Docker Desktop's Windows path mounts fail or are too slow. Install Ubuntu from WSL, install Docker *inside* that distro or point the Ubuntu agent at Docker Desktop's WSL integration, clone this repo, and run the same `minos-worker` commands as on Ubuntu. Paths are then POSIX and the Linux agent code is what actually runs.

## Persist the process

Task Scheduler: run `deployment/windows/register-worker-task.ps1` from an elevated PowerShell after editing the paths. The script registers a task that starts at logon. NSSM or WinSW are alternatives; the script does not install them.

Confirm, on that Windows host:

```text
minos-worker doctor --config worker.yaml
minos-worker start --config worker.yaml
```

A mock study should complete before you point the worker at an official study.
