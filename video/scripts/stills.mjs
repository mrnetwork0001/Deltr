import { bundle } from '@remotion/bundler';
import { renderStill, selectComposition } from '@remotion/renderer';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = process.argv[2] || resolve(HERE, '..', 'out', 'stills');
const frames = (process.argv[3] || '').split(',').filter(Boolean).map(Number);
const serveUrl = await bundle({ entryPoint: resolve(HERE, '..', 'src', 'index.ts'), publicDir: resolve(HERE, '..', 'public') });
const comp = await selectComposition({ serveUrl, id: 'Demo' });
console.log('composition', comp.durationInFrames, 'frames =', (comp.durationInFrames / comp.fps).toFixed(1), 's');
import { mkdirSync } from 'node:fs';
mkdirSync(OUT, { recursive: true });
for (const f of frames) { await renderStill({ composition: comp, serveUrl, output: `${OUT}/f${f}.png`, frame: f, imageFormat: 'png' }); console.log('still', f); }
