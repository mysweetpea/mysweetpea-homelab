// === Patch 5 v2: email agent — always read settings.json directly ===
// v1 only fixed the fallback; if the BaseAgent holds a cached wrong-but-truthy
// settings object (or the settings module hands a defaulted view), sends still
// broke with "Cannot read properties of undefined (reading 'smtpHost')". v2 routes
// every settings read in this file through a direct settings.json read (module
// fallback on error). NEVER exit(1): a failing patch must not block the boot chain.
const fs5 = require('fs');
const p5 = '/app/dist/lib/notifications/agents/email.js';
try {
  let src = fs5.readFileSync(p5, 'utf8');
  if (src.includes('MSP_PATCH5V2')) {
    console.log('PATCH 5v2 (email settings) ALREADY APPLIED');
  } else {
    const shortcut = 'if (this.settings) {\n            return this.settings;\n        }\n        ';
    const dropped = src.includes(shortcut);
    if (dropped) src = src.split(shortcut).join('');
    const call = '(0, settings_1.getSettings)()';
    const cnt = src.split(call).length - 1;
    src = src.split(call).join('__mspFileSettings()');
    const shim = '/* MSP_PATCH5V2: always resolve settings from settings.json (the module can hold a defaulted view) */\n' +
      'const __mspFileSettings = () => {\n' +
      '    try {\n' +
      '        const _f = require("fs"), _p = require("path");\n' +
      '        const sp = process.env.CONFIG_DIRECTORY ? _p.join(process.env.CONFIG_DIRECTORY, "settings.json") : _p.join(process.cwd(), "config", "settings.json");\n' +
      '        return JSON.parse(_f.readFileSync(sp, "utf8"));\n' +
      '    }\n' +
      '    catch (e) { return (0, settings_1.getSettings)(); }\n' +
      '};\n' +
      '/* MSP_PATCH5V2 */\n';
    const clsAnchor = 'class EmailAgent extends agent_1.BaseAgent {';
    if (!src.includes(clsAnchor)) throw new Error('class anchor not found');
    src = src.replace(clsAnchor, shim + clsAnchor);
    fs5.writeFileSync(p5, src);
    console.log('PATCH 5v2 OK (shortcut dropped: ' + dropped + ', calls re-routed: ' + cnt + ')');
  }
} catch (e) {
  console.log('PATCH 5v2 (non-fatal): ' + e.message);
}
