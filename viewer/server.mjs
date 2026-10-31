#!/usr/bin/env node
// server.mjs — the tiny dev server for the world-stream viewer.
//
// Static: the viewer page (+ vendored three.js) at /, the compiled world
// files under /build/. The API mirrors the shape core-spec gives the real
// service:
//
//   GET /api/scene                     → {branch, head, manifest_file}
//   GET /api/scene?since_version=N     → the since-N delta (op list) as JSON,
//                                        or {full: true, …} when the chain is
//                                        broken and the client must re-stream
//   GET /api/events                    → SSE; `world.dirty {branch, head}` on
//                                        every new version (the feed is the log)
//
// Demo mode (DEMO_DELAY_MS, default 6000; 0 disables): versions above the
// head at boot are held back and "land" once, DEMO_DELAY_MS after the first
// subscriber — so a fresh page shows the world changing while you watch.
//
//   node server.mjs [buildDir] [--port 8787]
//
// No dependencies; Node ≥ 18.

import { createServer } from 'node:http';
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const portFlag = args.indexOf('--port');
const PORT = portFlag !== -1 ? parseInt(args[portFlag + 1], 10) : 8787;
const positional = args.filter((a, i) => !a.startsWith('--') && i !== portFlag && i !== portFlag + 1);
const buildDir = resolve(positional[0] ?? join(here, 'build'));
const DEMO_DELAY_MS = parseInt(process.env.DEMO_DELAY_MS ?? '6000', 10);

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.glb': 'model/gltf-binary', '.png': 'image/png',
  '.ico': 'image/x-icon', '.map': 'application/json',
};

// ------------------------------------------------------- version discovery

function scanVersions() {
  const glbs = new Map();      // version → file
  const deltas = new Map();    // version → {file, since}
  const manifests = new Map(); // version → file
  let branch = 'main';
  try {
    for (const f of readdirSync(buildDir)) {
      let m;
      if ((m = f.match(/^world@(\d+)\.glb$/))) glbs.set(parseInt(m[1], 10), f);
      if ((m = f.match(/^world@(\d+)\.since(\d+)\.json$/))) deltas.set(parseInt(m[1], 10), { file: f, since: parseInt(m[2], 10) });
      if ((m = f.match(/^manifest@(\d+)\.json$/))) manifests.set(parseInt(m[1], 10), f);
    }
    if (existsSync(join(buildDir, 'manifest.json'))) {
      try {
        const mf = JSON.parse(readFileSync(join(buildDir, 'manifest.json'), 'utf8'));
        if (!manifests.has(mf.version)) manifests.set(mf.version, 'manifest.json');
        branch = mf.branch ?? branch;
      } catch { /* partial write — the next scan sees it whole */ }
    }
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  const head = Math.max(0, ...glbs.keys(), ...deltas.keys());
  return { glbs, deltas, manifests, head, branch };
}

let world = scanVersions();
const bootHead = world.head;
let announced = world.head; // versions above this are held back in demo mode
let demoTimer = null;

setInterval(() => {
  const next = scanVersions();
  const grew = next.head > world.head;
  world = next;
  if (!grew) return;
  if (DEMO_DELAY_MS === 0) releasePending();
  else if (subscribers.size > 0 && !demoTimer) {
    // a version that lands after the page subscribed still "lands" on screen
    demoTimer = setTimeout(() => { demoTimer = null; releasePending(); }, DEMO_DELAY_MS);
  }
}, 500);

function releasePending() {
  while (world.head > announced) {
    announced += 1;
    broadcast('world.dirty', { branch: world.branch, head: announced });
    console.log(`[world] dirty → v${announced} (${world.branch})`);
  }
}

// ------------------------------------------------------------------- SSE

const subscribers = new Set();

function broadcast(event, data) {
  for (const res of subscribers) {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  }
}

// ---------------------------------------------------------------- HTTP

function sendFile(res, path, type) {
  try {
    const body = readFileSync(path);
    res.writeHead(200, { 'content-type': type, 'content-length': body.length, 'cache-control': 'no-store' });
    res.end(body);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (url.pathname === '/api/scene') {
    const since = url.searchParams.get('since_version');
    if (since !== null) {
      const from = parseInt(since, 10);
      const delta = world.deltas.get(world.head);
      if (delta && delta.since === from && world.head > from) {
        const body = readFileSync(join(buildDir, delta.file));
        res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        res.end(body);
        return;
      }
      const manifestFile = world.manifests.get(world.head);
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify({
        full: true, branch: world.branch, head: world.head,
        manifest_file: manifestFile ?? null,
        reason: delta && delta.since !== from ? 'chain-broken' : 'no-delta',
      }));
      return;
    }
    res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
    res.end(JSON.stringify({
      branch: world.branch, head: world.head,
      manifest_file: world.manifests.get(world.head) ?? null,
    }));
    return;
  }

  if (url.pathname === '/api/events') {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-store',
      connection: 'keep-alive',
    });
    res.write(`retry: 1000\n\n`);
    res.write(`event: hello\ndata: ${JSON.stringify({ branch: world.branch, head: announced })}\n\n`);
    subscribers.add(res);
    if (DEMO_DELAY_MS > 0 && world.head > announced && !demoTimer) {
      demoTimer = setTimeout(() => { demoTimer = null; releasePending(); }, DEMO_DELAY_MS);
    }
    req.on('close', () => subscribers.delete(res));
    return;
  }

  if (url.pathname === '/') return sendFile(res, join(here, 'index.html'), MIME['.html']);
  if (url.pathname.startsWith('/build/')) {
    const file = join(buildDir, url.pathname.slice('/build/'.length));
    if (!file.startsWith(buildDir)) { res.writeHead(403).end(); return; }
    return sendFile(res, file, MIME[extname(file)] ?? 'application/octet-stream');
  }
  const staticFile = join(here, url.pathname.replace(/^\/+/, ''));
  if (staticFile.startsWith(here) && existsSync(staticFile) && statSync(staticFile).isFile()) {
    return sendFile(res, staticFile, MIME[extname(staticFile)] ?? 'application/octet-stream');
  }
  res.writeHead(404, { 'content-type': 'text/plain' });
  res.end('not found');
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`world-stream viewer dev server`);
  console.log(`  http://127.0.0.1:${PORT}/   (build dir: ${buildDir})`);
  console.log(`  versions: head=v${world.head} branch=${world.branch}` +
    (DEMO_DELAY_MS > 0 ? ` · demo: v${bootHead} → v${world.head} lands ${DEMO_DELAY_MS}ms after first subscriber` : ''));
});
