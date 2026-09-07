// Narration + sound effects through ElevenLabs. Credit-safe: quota check first, every file cached
// (re-runs never spend again), one take per item. The key is read from the Syntura env file, never logged.
import { existsSync, readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url));
const ENV_FILE = '/Users/mrnetwork/Syntura/video/.env';
const SCRIPT = JSON.parse(readFileSync(resolve(HERE, '..', 'narration', 'script.json'), 'utf8'));
const NARR = resolve(HERE, '..', 'public', 'narration');
const SFX = resolve(HERE, '..', 'public', 'sfx');
const RESERVE = 5000;
const SOUNDS = [
  { id: 'whoosh', text: 'short fast cinematic whoosh transition, clean, no reverb tail', seconds: 0.8 },
  { id: 'key', text: 'single mechanical keyboard key press, soft, close mic', seconds: 0.3 },
  { id: 'click', text: 'single soft UI click, subtle, modern interface', seconds: 0.3 },
  { id: 'ding', text: 'soft success chime, two ascending glassy notes, short, modern UI notification', seconds: 1.2 },
  { id: 'veto', text: 'short low error buzz, muted, modern UI denied sound', seconds: 0.6 },
  { id: 'pad', text: 'calm ambient electronic pad, warm, minimal, slow, seamless loop, no melody, no drums', seconds: 20 },
];
const env = {}; for (const raw of readFileSync(ENV_FILE, 'utf8').split('\n')) { const l = raw.trim(); if (!l || l.startsWith('#') || !l.includes('=')) continue; const [k, ...r] = l.split('='); env[k.trim()] = r.join('=').trim().replace(/^["']|["']$/g, ''); }
const key = env.ELEVENLABS_API_KEY; if (!key) throw new Error('no ELEVENLABS_API_KEY');
const H = { 'xi-api-key': key, 'content-type': 'application/json' };
mkdirSync(NARR, { recursive: true }); mkdirSync(SFX, { recursive: true });
const sub = await (await fetch('https://api.elevenlabs.io/v1/user/subscription', { headers: H })).json();
const remaining = sub.character_limit - sub.character_count;
const pendingN = SCRIPT.sections.filter((s) => !existsSync(`${NARR}/${s.id}.mp3`));
const pendingS = SOUNDS.filter((s) => !existsSync(`${SFX}/${s.id}.mp3`));
const cost = pendingN.reduce((n, s) => n + s.text.length, 0) + pendingS.length * 200;
console.log(`tier ${sub.tier}: ${remaining} remaining; ${pendingN.length} narration + ${pendingS.length} sfx to make, ~${cost} chars`);
if (cost > remaining - RESERVE) throw new Error('refusing: not enough quota');
for (const s of pendingN) {
  const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${SCRIPT.voice}?output_format=mp3_44100_128`, { method: 'POST', headers: H, body: JSON.stringify({ text: s.text, model_id: SCRIPT.model, voice_settings: { stability: 0.5, similarity_boost: 0.8, style: 0.15, use_speaker_boost: true } }) });
  if (!r.ok) throw new Error(`tts ${s.id}: ${r.status} ${(await r.text()).slice(0, 200)}`);
  writeFileSync(`${NARR}/${s.id}.mp3`, Buffer.from(await r.arrayBuffer())); console.log('narration', s.id, 'ok');
}
for (const s of pendingS) {
  const r = await fetch('https://api.elevenlabs.io/v1/sound-generation', { method: 'POST', headers: H, body: JSON.stringify({ text: s.text, duration_seconds: s.seconds, prompt_influence: 0.5, ...(s.id === 'pad' ? { loop: true } : {}) }) });
  if (!r.ok) { console.log('sfx', s.id, 'failed', r.status, (await r.text()).slice(0, 120), '-> synthesized fallback stays'); continue; }
  writeFileSync(`${SFX}/${s.id}.mp3`, Buffer.from(await r.arrayBuffer())); console.log('sfx', s.id, 'ok');
}
