// === Patch 5: email notification agent — read agent settings from settings.json ===
// The shared settings module can hand the email agent a stale/defaulted view
// (options missing -> "Cannot read properties of undefined (reading 'smtpHost')").
// The email agent is the only consumer of these values, so read the file directly.
// NEVER exit(1): a failing patch must not block the boot chain.
//
// Deployed to the seerr PVC at /app/config/patch5.js (like patch4.js); the boot
// chain in apps/dmz/seerr/values.yaml runs it before dist/index.js.
const fs5 = require('fs');
const path5 = require('path');
const p5 = '/app/dist/lib/notifications/agents/email.js';
try {
  let src = fs5.readFileSync(p5, 'utf8');
  if (src.includes('MSP_PATCH5')) {
    console.log('PATCH 5 (email settings) ALREADY APPLIED');
  } else {
    const anchor = 'const settings = (0, settings_1.getSettings)();\n        return settings.notifications.agents.email;';
    const idx = src.indexOf(anchor);
    if (idx === -1) throw new Error('email getSettings anchor not found');
    const repl = '/* MSP_PATCH5: read agent settings straight from settings.json (settings module may hold a defaulted view) */\n' +
      '        const _fs5 = require("fs");\n' +
      '        const _path5 = require("path");\n' +
      '        const _sp5 = process.env.CONFIG_DIRECTORY ? _path5.join(process.env.CONFIG_DIRECTORY, "settings.json") : _path5.join(process.cwd(), "config", "settings.json");\n' +
      '        return JSON.parse(_fs5.readFileSync(_sp5, "utf8")).notifications.agents.email;';
    src = src.slice(0, idx) + repl + src.slice(idx + anchor.length);
    fs5.writeFileSync(p5, src);
    console.log('PATCH 5 (email settings) OK');
  }
} catch (e) {
  console.log('PATCH 5 (non-fatal): ' + e.message);
}
