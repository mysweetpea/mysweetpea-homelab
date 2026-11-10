#!/usr/bin/env python3
"""Regenerate the Open WebUI index.html ConfigMap from the running image.

The ConfigMap pins index.html, which hardcodes SvelteKit's hashed entry
chunks. Every image bump changes those hashes, so a stale ConfigMap makes the
SPA fail to boot (splash screen forever, no login form). Run this after ANY
image tag change in values.yaml:

    ./regen-index.py v0.13.3        # writes web-index-configmap.yaml

It pulls the pristine index.html out of the image (throwaway pod, no mount),
re-applies the MySweetPea customizations (title + SSO login hint), and rewrites
the ConfigMap as a JSON-escaped one-liner (block scalars can't hold the tabs).
"""
import json
import subprocess
import sys

IMAGE = 'ghcr.io/open-webui/open-webui:{}'
NS = 'private'
CM_FILE = 'web-index-configmap.yaml'

DISCLAIMER = (
    '<div id="mysweetpea-login-disclaimer" style="display:none;position:fixed;'
    'bottom:0;left:0;right:0;z-index:9999;background:rgba(16,16,16,0.94);'
    'color:rgba(255,255,255,0.85);text-align:center;padding:10px 18px;'
    "font-family:'Segoe UI','Helvetica Neue',sans-serif;font-size:13px;"
    'line-height:1.45;border-top:1px solid rgba(255,255,255,0.12);'
    'backdrop-filter:blur(4px);">Sign in with your MySweetPea account '
    '(Authentik SSO). Use the same username and password as Vaultwarden, '
    'AFFiNE, and the other services.</div>\n'
    '\t<script>\n'
    '\t\t(function () {\n'
    '\t\t\tfunction hasSession() {\n'
    "\t\t\t\ttry { return !!localStorage.getItem('token'); } catch (e) { return false; }\n"
    '\t\t\t}\n'
    '\t\t\tfunction update() {\n'
    "\t\t\t\tvar el = document.getElementById('mysweetpea-login-disclaimer');\n"
    "\t\t\t\tif (el) el.style.display = hasSession() ? 'none' : 'block';\n"
    '\t\t\t}\n'
    '\t\t\tupdate();\n'
    '\t\t\tsetInterval(update, 1500);\n'
    '\t\t})();\n'
    '\t</script>\n'
)


def fetch_index(tag):
    pod = 'owui-idx'
    subprocess.run(['kubectl', '-n', NS, 'delete', 'pod', pod, '--ignore-not-found'],
                   check=False, capture_output=True)
    subprocess.run(['kubectl', '-n', NS, 'run', pod, '--image', IMAGE.format(tag),
                    '--restart=Never', '--command', '--', 'cat', '/app/build/index.html'],
                   check=True, capture_output=True)
    import time
    html = ''
    for _ in range(40):                       # pod runs `cat`, so wait for logs
        r = subprocess.run(['kubectl', '-n', NS, 'logs', pod],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            html = r.stdout
            break
        time.sleep(2)
    if not html:
        raise SystemExit('could not read index.html from ' + pod)
    subprocess.run(['kubectl', '-n', NS, 'delete', 'pod', pod, '--ignore-not-found'],
                   check=False, capture_output=True)
    return html


def patch(html):
    if '<title>Open WebUI</title>' not in html:
        raise SystemExit('unexpected index.html: no stock <title>')
    html = html.replace('<title>Open WebUI</title>', '<title>MySweetPea AI</title>')
    if '</body>' not in html:
        raise SystemExit('unexpected index.html: no </body>')
    html = html.replace('\t</body>', DISCLAIMER + '\t</body>', 1)
    return html


def write_cm(html):
    doc = (
        'apiVersion: v1\n'
        'kind: ConfigMap\n'
        'metadata:\n'
        '  name: open-webui-web-index\n'
        '  namespace: private\n'
        '  labels:\n'
        '    app.kubernetes.io/name: open-webui\n'
        '    app.kubernetes.io/part-of: homelab\n'
        'data:\n'
        '  # REGENERATE ON EVERY IMAGE BUMP: ./regen-index.py <new tag>\n'
        '  # A stale copy pins dead /_app/immutable/entry/*.js hashes and the\n'
        '  # SPA never boots (splash screen forever, no login button).\n'
        f'  index.html: {json.dumps(html)}\n'
    )
    open(CM_FILE, 'w').write(doc)


def selftest(html):
    assert '<title>MySweetPea AI</title>' in html
    assert 'mysweetpea-login-disclaimer' in html
    entry = html.split('/_app/immutable/entry/start.')[1].split('"')[0]
    assert len(entry) > 5, entry
    print('selftest ok — entry chunk start.' + entry)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    html = patch(fetch_index(sys.argv[1]))
    selftest(html)
    write_cm(html)
    print(f'wrote {CM_FILE} ({len(html)} bytes of html)')
