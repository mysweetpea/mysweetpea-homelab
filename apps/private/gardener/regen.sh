#!/usr/bin/env bash
# Re-embed gardener.py into the ConfigMap inside manifests/gardener.yaml.
# Run after every edit to gardener.py, then commit both files together.
set -euo pipefail
cd "$(dirname "$0")"
python3 - <<'PY'
src = open('gardener.py').read().rstrip('\n')
block = '\n'.join(('      ' + l) if l.strip() else '' for l in src.split('\n'))
m = open('manifests/gardener.yaml').read()
head, rest = m.split('  gardener.py: |\n', 1)
_, tail = rest.split('\n---\n', 1)
open('manifests/gardener.yaml','w').write(head + '  gardener.py: |\n' + block + '\n---\n' + tail)
print('re-embedded gardener.py')
PY
