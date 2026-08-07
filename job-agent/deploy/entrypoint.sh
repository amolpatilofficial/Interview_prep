#!/bin/sh
# Fail loudly on the two misconfigurations that matter, before anything binds a port.
set -e

if [ -z "$JAA_AUTH_PASSWORD" ]; then
  echo "FATAL: JAA_AUTH_PASSWORD is not set." >&2
  echo "       This container listens on 0.0.0.0 and holds your resume and" >&2
  echo "       personal details. Set a password before starting it:" >&2
  echo "         JAA_AUTH_PASSWORD=\$(openssl rand -base64 24)" >&2
  exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "FATAL: ANTHROPIC_API_KEY is not set." >&2
  exit 1
fi

if [ ! -f "${JAA_PROFILE_PATH:-/data/profile.yaml}" ]; then
  echo "WARNING: no profile at ${JAA_PROFILE_PATH:-/data/profile.yaml}." >&2
  echo "         Copy config/profile.example.yaml into the data volume and edit it;" >&2
  echo "         runs will fail until you do." >&2
fi

exec "$@"
