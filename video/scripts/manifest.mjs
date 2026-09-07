// Builds public/manifest.json: real durations of every narration file and clip (ffprobe) plus the
// captured event timelines, so the composition's timing is derived, never guessed.
import { execSync } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url));
const PUB = resolve(HERE, '..', 'public');
const dur = (f) => parseFloat(execSync(`ffprobe -v error -show_entries format=duration -of csv=p=0 "${f}"`).toString());
const script = JSON.parse(readFileSync(resolve(HERE, '..', 'narration', 'script.json'), 'utf8'));
const narration = {};
for (const s of script.sections) narration[s.id] = { file: `narration/${s.id}.mp3`, seconds: dur(`${PUB}/narration/${s.id}.mp3`), text: s.text };
const footage = {};
for (const n of ['landing', 'paper', 'live']) {
  const ev = JSON.parse(readFileSync(`${PUB}/footage/${n}.events.json`, 'utf8'));
  footage[n] = { file: `footage/${n}.mp4`, seconds: dur(`${PUB}/footage/${n}.mp4`), events: ev.events };
}
const sfx = {};
for (const id of ['whoosh', 'key', 'click', 'ding', 'veto', 'pad']) {
  const mp3 = `${PUB}/sfx/${id}.mp3`, wav = `${PUB}/sfx/${id}.wav`;
  const f = existsSync(mp3) ? mp3 : wav; sfx[id] = { file: `sfx/${id}.${existsSync(mp3) ? 'mp3' : 'wav'}`, seconds: dur(f) };
}
writeFileSync(`${PUB}/manifest.json`, JSON.stringify({ narration, footage, sfx }, null, 1));
console.log('narration s:', Object.fromEntries(Object.entries(narration).map(([k, v]) => [k, +v.seconds.toFixed(1)])));
console.log('footage s:', Object.fromEntries(Object.entries(footage).map(([k, v]) => [k, +v.seconds.toFixed(1)])));
console.log('sfx:', Object.fromEntries(Object.entries(sfx).map(([k, v]) => [k, v.file.split('/')[1] + ' ' + v.seconds.toFixed(2)])));
