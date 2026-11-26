#!/usr/bin/env bash
# Keep the lookup order in sync with runtime_root.py.
fertile_root() {
  if [[ -n ${FERTILE_ROOT:-} ]]; then
    printf '%s\n' "$FERTILE_ROOT"
  elif [[ -n ${FERTILE_BOX_HOME:-} ]]; then
    printf '%s/fertile\n' "${FERTILE_BOX_HOME%/}"
  elif [[ ${FERTILE_PROFILE:-} == stable ]]; then
    printf '%s\n' /var/lib/fertile
  else
    printf '%s\n' /home/box/fertile
  fi
}
