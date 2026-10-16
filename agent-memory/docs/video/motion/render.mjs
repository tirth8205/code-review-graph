import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';
import {copyFile, mkdir, readFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {tmpdir, availableParallelism} from 'node:os';
import {spawnSync} from 'node:child_process';

const here = dirname(fileURLToPath(import.meta.url));
const videoDir = resolve(here, '..');
const scratch = join(tmpdir(), 'code-review-graph-motion-render');
const publicDir = join(scratch, 'public');
const browserExecutable = process.env.VIDEO_CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
await mkdir(publicDir, {recursive: true});
for (const name of ['voiceover.wav']) {
  await copyFile(join(videoDir, name), join(publicDir, name));
}
for (const name of ['DMSans-Regular.ttf', 'DMSans-Bold.ttf']) {
  await copyFile(join('/Users/tirth/Library/Fonts', name), join(publicDir, name));
}
const seconds = (time) => {
  const [h, m, tail] = time.replace(',', '.').split(':');
  return Number(h) * 3600 + Number(m) * 60 + Number(tail);
};
const captions = (await readFile(join(videoDir, 'voiceover.srt'), 'utf8')).trim().split(/\r?\n\r?\n/).map((block) => {
  const [, times, ...lines] = block.split(/\r?\n/);
  const [start, end] = times.split(' --> ').map(seconds);
  return {start, end, text: lines.join('\n')};
});
const inputProps = {captions};
const serveUrl = await bundle({entryPoint: join(here, 'index.jsx'), outDir: join(scratch, 'bundle'), publicDir, enableCaching: true, onProgress: (n) => {if (n === 100) console.log('Bundle ready');}});
const shared = {serveUrl, inputProps, browserExecutable, chromeMode: 'chrome-for-testing', logLevel: 'warn'};
const composition = await selectComposition({...shared, id: 'AgentMemory4K'});
if (process.argv.includes('--samples')) {
  for (const time of [3.5, 13.5, 22, 33.5, 44.5, 53.5, 58.5]) {
    const output = join(videoDir, `motion-frame-${time}.png`);
    await renderStill({...shared, composition, frame: time * 24, output, imageFormat: 'png'});
    console.log(`Sample saved: ${output}`);
  }
}
if (!process.argv.includes('--samples') || process.argv.includes('--render-after-samples')) {
  let lastPercent = -1;
  const outputLocation = join(scratch, 'rendered-motion.mp4');
  await renderMedia({...shared, composition, outputLocation, codec: 'h264', crf: 18, x264Preset: 'veryfast', pixelFormat: 'yuv420p', imageFormat: 'jpeg', jpegQuality: 92, audioCodec: 'aac', audioBitrate: '192k', concurrency: Math.min(10, availableParallelism()), timeoutInMilliseconds: 60000,
    metadata: {title: 'code-review-graph: Agent memory', comment: 'Illustrative demo. Framer Motion camera with Remotion. No live agent or paid-provider footage.'},
    onProgress: ({progress}) => {const percent = Math.floor(progress * 100); if (percent >= lastPercent + 5) {console.log(`Render ${percent}%`); lastPercent = percent;}}
  });
  // AAC packet padding can make Remotion's container 11 ms longer than the
  // 1440-frame composition. Final mux sets the authored WAV and exact 60 s.
  const finalOutput = join(videoDir, 'code-review-graph-agent-memory-4k.mp4');
  const mux = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y',
    '-i', outputLocation, '-i', process.env.VIDEO_VOICEOVER_PATH || join(videoDir, 'voiceover.wav'),
    '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
    '-b:a', '192k', '-t', '60', '-movflags', '+faststart',
    '-metadata', 'title=code-review-graph: Agent memory',
    '-metadata', 'comment=Illustrative demo. Animated product interactions with Framer Motion and Remotion. AI narration: British female voice.',
    finalOutput], {stdio: 'inherit'});
  if (mux.status !== 0) throw new Error('Final 60-second audio mux failed');
  console.log(`Saved: ${finalOutput}`);
}
