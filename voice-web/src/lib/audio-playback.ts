interface ScheduledSource {
  source: AudioBufferSourceNode;
  startTime: number;
}

let audioCtx: AudioContext | null = null;
let gainNode: GainNode | null = null;
let scheduledSources: ScheduledSource[] = [];
let nextStartTime = 0;

export function createPlayer(): void {
  audioCtx = new AudioContext();
  gainNode = audioCtx.createGain();
  gainNode.connect(audioCtx.destination);
  nextStartTime = 0;
}

export function enqueue(pcmData: Uint8Array): void {
  if (!audioCtx || !gainNode) return;

  // Copy to aligned buffer (pako output may have non-aligned byteOffset)
  const aligned = new Uint8Array(pcmData);
  const int16 = new Int16Array(aligned.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768;
  }

  const buffer = audioCtx.createBuffer(1, float32.length, 24000);
  buffer.getChannelData(0).set(float32);

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(gainNode);

  const now = audioCtx.currentTime;
  if (nextStartTime < now) {
    nextStartTime = now;
  }
  source.start(nextStartTime);

  scheduledSources.push({ source, startTime: nextStartTime });
  nextStartTime += buffer.duration;

  scheduledSources = scheduledSources.filter((s) => s.startTime + 1 > now);
}

export function clearPlayback(): void {
  if (!audioCtx || !gainNode) return;

  gainNode.gain.setValueAtTime(0, audioCtx.currentTime);

  for (const s of scheduledSources) {
    try {
      s.source.stop();
      s.source.disconnect();
    } catch {
      // Already stopped
    }
  }
  scheduledSources = [];
  nextStartTime = 0;

  gainNode.gain.setValueAtTime(1, audioCtx.currentTime + 0.05);
}

export function closePlayer(): void {
  clearPlayback();
  if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }
  gainNode = null;
}
