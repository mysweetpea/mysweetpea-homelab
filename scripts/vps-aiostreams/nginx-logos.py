#!/usr/bin/env python3
"""Insert the /logos/ static route into the aiostreams vhost (idempotent)."""
p = "/etc/nginx/sites-enabled/aiostreams"
s = open(p).read()
if "location ^~ /logos/" in s:
    print("logos location already present - no change")
else:
    marker = "    location ^~ /avatars/"
    assert marker in s, "avatars marker missing - aborting"
    block = (
        "    location ^~ /logos/ {\n"
        "        root /var/www/aiostreams;\n"
        '        add_header Cache-Control "public, max-age=604800";\n'
        "        access_log off;\n"
        "    }\n\n"
    )
    open(p + ".bak-logos-20261016", "w").write(s)
    open(p, "w").write(s.replace(marker, block + marker, 1))
    print("inserted /logos/ location (backup .bak-logos-20261016)")
