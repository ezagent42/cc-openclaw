export type OnChunkCallback = (pcmChunk: Uint8Array) => void;

let audioContext: AudioContext | null = null;
let mediaStream: MediaStream | null = null;
let sourceNode: MediaStreamAudioSourceNode | null = null;
let workletNode: AudioWorkletNode | null = null;

export async function startCapture(onChunk: OnChunkCallback): Promise<void> {
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      sampleRate: 16000,
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });

  audioContext = new AudioContext({ sampleRate: 16000 });
  await audioContext.audioWorklet.addModule('/pcm-processor.js');

  sourceNode = audioContext.createMediaStreamSource(mediaStream);
  workletNode = new AudioWorkletNode(audioContext, 'pcm-processor');

  workletNode.port.onmessage = (event: MessageEvent<Uint8Array>) => {
    onChunk(event.data);
  };

  sourceNode.connect(workletNode);
  workletNode.connect(audioContext.destination);
}

export function stopCapture(): void {
  if (workletNode) {
    workletNode.disconnect();
    workletNode = null;
  }
  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
}
