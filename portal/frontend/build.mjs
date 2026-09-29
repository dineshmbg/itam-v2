// Offline build: bundles JS + CSS with esbuild, inlines the Carbon icon sprite, hashes assets for immutable caching.
// Usage: node build.mjs            (no network needed - every dependency is in ./node_modules and ../vendor)
import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = path.resolve(import.meta.dirname);
const outDir = path.resolve(root, '../static');
const assets = path.join(outDir, 'assets');
const tmp = path.join(root, '.tmp-icons');
const carbonTgz = path.resolve(root, '../vendor/npm/carbon-icons-11.88.0.tgz');

// ---------------------------------------------------------------- icons (Carbon, Apache-2.0) -> one inline SVG sprite
const ICONS = `menu search close chevron--down chevron--up chevron--right chevron--left arrow--up arrow--down arrow--right chevron--sort chevron--sort--up
chevron--sort--down filter filter--reset restart launch information warning help keyboard settings light asleep screen checkmark--filled checkmark--outline
close--filled close--outline error--filled warning--filled warning--alt--filled information--filled radio-button radio-button--checked checkbox checkbox--checked
checkbox--checked--filled circle-dash pending in-progress time timer hourglass gender--male gender--female user user--avatar user--multiple laptop devices printer
qr-code barcode network--3 server--dns battery--full scan data--base tools package box archive tag location calendar report chart--bar chart--column chart--line security wifi cube
document catalog data-table dashboard analytics renew copy download phone
edit add save undo camera arrow--left trash-can locked unlocked reset view view--off logout link email send upload folder play pause stop email--new alarm calendar--heat-map notification checkmark`.split(/\s+/).filter(Boolean);

async function spriteSvg() {
  fs.mkdirSync(tmp, { recursive: true });
  const need = ICONS.map((n) => `package/es/${n}/16.js`);
  const relTgz = path.relative(tmp, carbonTgz).split(path.sep).join('/'); // relative: GNU tar treats 'C:' as a remote host
  execFileSync('tar', ['-xzf', relTgz, 'package/LICENSE', ...need], { stdio: 'pipe', cwd: tmp });
  const symbols = [];
  for (const name of ICONS) {
    const src = fs.readFileSync(path.join(tmp, 'package/es', name, '16.js'), 'utf8').replace('export { _16_default as default };', 'export default _16_default;');
    const mod = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
    const d = mod.default;
    const attrs = (a) => Object.entries(a).map(([k, v]) => ` ${k}="${String(v).replace(/"/g, '&quot;')}"`).join('');
    const inner = d.content.map((c) => `<${c.elem}${attrs(c.attrs || {})}/>`).join('');
    symbols.push(`<symbol id="i-${name}" viewBox="${d.attrs.viewBox}">${inner}</symbol>`);
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true">${symbols.join('')}</svg>`;
}

// ---------------------------------------------------------------- clean previous generated files (non-recursive, generated names only)
fs.mkdirSync(assets, { recursive: true });
for (const dir of [assets, path.join(assets, 'fonts')]) {
  if (!fs.existsSync(dir)) continue;
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f);
    if (fs.statSync(p).isFile() && /-[A-Z0-9]{8}\.[a-z0-9.]+$/.test(f)) fs.unlinkSync(p);
  }
}

const common = { bundle: true, minify: true, sourcemap: true, target: 'es2022', logLevel: 'warning', metafile: true, outdir: assets, absWorkingDir: root };

const js = await build({
  ...common, format: 'esm', splitting: true, entryPoints: { app: 'src/js/main.js' },
  entryNames: '[name]-[hash]', chunkNames: 'chunk-[hash]',
});
const boot = await build({ ...common, format: 'iife', splitting: false, entryPoints: { boot: 'src/js/boot.js' }, entryNames: '[name]-[hash]' });
const css = await build({
  ...common, entryPoints: { app: 'src/css/main.css' }, entryNames: '[name]-[hash]', assetNames: 'fonts/[name]-[hash]',
  loader: { '.woff2': 'file' }, publicPath: '/static/assets',
});

const find = (meta, ext, entry) => Object.entries(meta.outputs).find(([k, v]) => k.endsWith(ext) && v.entryPoint?.includes(entry))?.[0];
const rel = (p) => '/static/assets/' + path.basename(p);
const jsFile = rel(find(js.metafile, '.js', 'main.js'));
const bootFile = rel(find(boot.metafile, '.js', 'boot.js'));
const cssFile = rel(find(css.metafile, '.css', 'main.css'));
const fontFiles = Object.keys(css.metafile.outputs).filter((k) => k.endsWith('.woff2'));
const preload = fontFiles
  .filter((f) => /ibm-plex-sans-latin-(400|600)-normal/.test(f))
  .map((f) => `<link rel="preload" href="/static/assets/fonts/${path.basename(f)}" as="font" type="font/woff2" crossorigin>`).join('\n');

let html = fs.readFileSync(path.join(root, 'src/index.html'), 'utf8');
html = html
  .replace('<!--BOOT-->', `<script src="${bootFile}"></script>`)
  .replace('<!--PRELOAD-->', preload)
  .replace('<!--CSS-->', `<link rel="stylesheet" href="${cssFile}">`)
  .replace('<!--JS-->', `<script type="module" src="${jsFile}"></script>`)
  .replace('<!--SPRITE-->', await spriteSvg());
fs.writeFileSync(path.join(outDir, 'index.html'), html);
fs.copyFileSync(path.join(root, 'src/favicon.svg'), path.join(outDir, 'favicon.svg'));
fs.copyFileSync(path.join(root, 'src/ongc-logo.png'), path.join(assets, 'ongc-logo.png'));
fs.copyFileSync(path.join(tmp, 'package/LICENSE'), path.join(assets, 'LICENSE-carbon-icons.txt'));

const size = (f) => (fs.statSync(path.join(outDir, f.replace('/static/', ''))).size / 1024).toFixed(1) + ' KB';
console.log('built:', { js: jsFile + ' ' + size(jsFile), css: cssFile + ' ' + size(cssFile), boot: bootFile + ' ' + size(bootFile), fonts: fontFiles.length });
