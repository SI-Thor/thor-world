// Screenshot driver for the world-stream viewer (headless Chromium via the
// playwright-core bundled with zcode). Usage:
//   node ws-shot.mjs <url> <outPrefix> <deltaFile> <buildDir>
// Loads the page, waits for the world to render, drops the delta file into
// the build dir (the live edit "lands"), waits for the version chip to flip,
// screenshots before/after.
import { copyFileSync } from 'node:fs';
import { join } from 'node:path';

const [url, outPrefix, deltaFile, buildDir] = process.argv.slice(2);
const { chromium } = await import('/home/proart/.nvm/versions/node/v24.21.0/lib/node_modules/zcode-app-cli/node_modules/playwright-core/index.mjs');

const browser = await chromium.launch({
  executablePath: '/home/proart/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome',
  headless: true,
  args: ['--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
page.on('console', (m) => console.log('[console]', m.type(), m.text()));
page.on('pageerror', (e) => console.log('[pageerror]', e.message));

await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => document.querySelector('#stats')?.textContent.includes('templates'), null, { timeout: 60000 });
if (process.env.FRAMING) {
  const parts = process.env.FRAMING.split(',').map(Number);
  const [dist, theta, phi, tx, ty, tz] = parts;
  await page.evaluate(([dist, theta, phi, tx, ty, tz]) => {
    const o = window.worldStream.orbit;
    if (Number.isFinite(dist)) o.dist = dist;
    if (Number.isFinite(theta)) o.theta = theta;
    if (Number.isFinite(phi)) o.phi = phi;
    if (Number.isFinite(tx)) o.target.set(tx, ty, tz);
  }, [dist, theta, phi, tx, ty, tz]);
}
await page.waitForTimeout(2500); // a few rendered frames

const badge = await page.textContent('#backend');
const stats = await page.textContent('#stats');
const v1 = await page.textContent('#version');
console.log(JSON.stringify({ badge, stats, version: v1 }));
await page.screenshot({ path: `${outPrefix}-v1.png` });

if (deltaFile && buildDir) {
  copyFileSync(deltaFile, join(buildDir, deltaFile.split('/').pop()));
  await page.waitForFunction(() => document.querySelector('#version')?.classList.contains('dirty'), null, { timeout: 20000 });
  await page.waitForTimeout(800);
  const card = await page.textContent('#log').catch(() => '(no log)');
  console.log(JSON.stringify({ versionAfter: await page.textContent('#version'), ledger: card }));
  await page.screenshot({ path: `${outPrefix}-v2.png` });
}
await browser.close();
