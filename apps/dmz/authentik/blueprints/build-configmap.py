#!/usr/bin/env python3
"""
build-configmap.py
==================
Generate the `additionalObjects` block for authentik `values.yaml` from the
sanitized blueprint files in this directory.

Authentik's Helm chart mounts blueprints from a ConfigMap via
`blueprints.configMaps`. Because the authentik ArgoCD Application is a Helm
chart source (not a directory source), the blueprint ConfigMap must be
rendered through the chart's `additionalObjects` field in `values.yaml`.

This script reads every `*.sanitized.yaml` file in this directory and prints
the `additionalObjects` YAML block. Paste the output into
`apps/dmz/authentik/values.yaml` under the `additionalObjects:` key.

Usage:
    python3 build-configmap.py

Prerequisites:
    1. Export each object from the Authentik UI (the `{ }` icon).
    2. Run `python3 sanitize-blueprints.py <exported>.yaml` to produce
       `<exported>.yaml.sanitized.yaml`.
    3. Run this script and paste the output into values.yaml.
    4. Populate the Kubernetes Secret `authentik-blueprint-env` with the
       env vars the sanitizer reported (AUTHENTIK_BP_*).
"""

from pathlib import Path

HERE = Path(__file__).parent
CONFIGMAP_NAME = "authentik-blueprints"
NAMESPACE = "dmz"

# Include authored blueprint files (00-groups.yaml, 01-providers.yaml, etc.).
# These are already sanitized (secrets use !Env tags). Excludes this script and
# any *.sanitized.yaml artifacts produced by sanitize-blueprints.py.
BLUEPRINT_GLOB = "*.yaml"


def main() -> None:
    files = sorted(HERE.glob(BLUEPRINT_GLOB))
    if not files:
        print("# No *.sanitized.yaml files found. Export + sanitize blueprints first.")
        return

    print("additionalObjects:")
    print("- apiVersion: v1")
    print("  kind: ConfigMap")
    print("  metadata:")
    print(f"    name: {CONFIGMAP_NAME}")
    print(f"    namespace: {NAMESPACE}")
    print("  data:")
    for f in files:
        # Authentik only discovers keys ending in .yaml.
        # Filename is <name>.yaml -> key stays <name>.yaml
        key = f.name
        content = f.read_text()
        indented = "\n".join(("      " + line) if line.strip() else "" for line in content.splitlines())
        print(f"    {key}: |")
        print(indented)


if __name__ == "__main__":
    main()
