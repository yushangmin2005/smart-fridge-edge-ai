#!/usr/bin/env bash

load_config_with_overrides() {
  local config_file="$1"
  shift

  local overrides=()
  local name
  for name in "$@"; do
    if [ "${!name+x}" = "x" ]; then
      overrides+=("$name=${!name}")
    fi
  done

  set -a
  # shellcheck disable=SC1090
  . "$config_file"
  set +a

  local assignment
  for assignment in "${overrides[@]}"; do
    export "$assignment"
  done
}
