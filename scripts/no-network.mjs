// Test-process guard and bounded audit. Not an OS-wide network monitor.
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import tls from 'node:tls';
import { writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
const attempts = [];
const deny = name => () => { attempts.push(name); throw new Error(`PM0-03 network forbidden: ${name}`); };
globalThis.fetch = deny('global.fetch');
http.request = deny('http.request'); http.get = deny('http.get');
https.request = deny('https.request'); https.get = deny('https.get');
net.Socket.prototype.connect = deny('net.Socket.connect');
tls.connect = deny('tls.connect');
syncBuiltinESMExports();
process.on('exit', () => {
  if (process.env.PI_PM003_AUDIT_PATH) writeFileSync(process.env.PI_PM003_AUDIT_PATH, JSON.stringify({
    scope: 'test child process only: global.fetch, node:http/https request/get, net.Socket.connect, tls.connect; not all native/OS networking',
    attempts: attempts.length, observedEntryPoints: attempts, externalModelCalls: 0,
    modelCallBasis: 'only injected deterministic stream; no registered live provider or credential resolution',
  }, null, 2));
  if (attempts.length) process.exitCode = 1;
});
