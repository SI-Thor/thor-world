// Regression test for viewer/server.mjs bug fixed in commit 8d58b19
// Ensure the server never reports a null manifest_file even when the head is a delta.

import { test, before, after } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createServer as netCreateServer } from 'node:net';
import { request } from 'node:http';

// Helper to perform an HTTP GET and return parsed JSON.
function httpGetJson(port, path) {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port, path, method: 'GET' }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => (data += chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on('error', reject);
    req.end();
  });
}

let tempDir;
let serverProc;
let port;

before(async () => {
  // Create temporary build directory with required files.
  tempDir = mkdtempSync(join(tmpdir(), 'viewer-build-'));
  // Minimal placeholder GLB (binary) – a short string is fine.
  writeFileSync(join(tempDir, 'world@1.glb'), 'placeholder');
  // Minimal manifest JSON.
  writeFileSync(join(tempDir, 'manifest@1.json'), JSON.stringify({ version: 1, branch: 'main' }));
  // Delta JSON with ops array.
  writeFileSync(
    join(tempDir, 'world@2.since1.json'),
    JSON.stringify({ ops: [{ op: 'noop' }] })
  );

  // Find a free port.
  const s = netCreateServer();
  await new Promise((res) => s.listen(0, '127.0.0.1', res));
  port = s.address().port;
  s.close();

  // Start the server.
  serverProc = spawn('node', ['viewer/server.mjs', tempDir, '--port', `${port}`], {
    env: { ...process.env, DEMO_DELAY_MS: '0' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  // Wait until the server prints its listening line.
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Server did not start in time')),
      5000);
    serverProc.stdout.on('data', (chunk) => {
      const line = chunk.toString();
      if (line.includes(`http://127.0.0.1:${port}/`)) {
        clearTimeout(timeout);
        resolve();
      }
    });
    serverProc.on('error', reject);
  });
});

after(() => {
  if (serverProc && !serverProc.killed) serverProc.kill();
  if (tempDir) rmSync(tempDir, { recursive: true, force: true });
});

test('GET /api/scene returns a manifest and non‑null manifest_file', async () => {
  const scene = await httpGetJson(port, '/api/scene');
  // The server should report the first full manifest.
  assert.strictEqual(scene.manifest_file, 'manifest@1.json');
  // Head may be 1 (if the delta is not announced) or 2 (if announced).
  assert.ok(scene.head === 1 || scene.head === 2, `unexpected head ${scene.head}`);
});

test('GET /api/scene?since_version=1 returns the delta JSON', async () => {
  const delta = await httpGetJson(port, '/api/scene?since_version=1');
  assert.ok(Array.isArray(delta.ops), 'delta should contain an ops array');
});
