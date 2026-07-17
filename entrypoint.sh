#!/bin/sh
# If an SSH key source was mounted read-only (see docker-compose.local.yml's
# commented .ssh-src volume line), copy it into this container's own
# writable ~/.ssh and fix permissions before starting the app.
#
# Why this exists: bind-mounting a Windows host folder straight into
# ~/.ssh usually carries overly-permissive Windows ACLs (no real POSIX
# permission bits on NTFS), and OpenSSH's client refuses to use a private
# key file it considers too open ("UNPROTECTED PRIVATE KEY FILE!",
# connection refused). Copying into a container-local, container-owned
# directory and chmod'ing it there sidesteps the problem entirely, so the
# Docker path doesn't hit a permissions error the venv/deploy.ps1 path
# never has (Windows' own ssh.exe has no such check).
set -e

if [ -d /home/appuser/.ssh-src ]; then
    mkdir -p /home/appuser/.ssh
    cp -r /home/appuser/.ssh-src/. /home/appuser/.ssh/
    chmod 700 /home/appuser/.ssh
    find /home/appuser/.ssh -type f -exec chmod 600 {} \;
fi

exec "$@"
