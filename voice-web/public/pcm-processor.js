class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(320);
    this.writeIndex = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;

    for (let i = 0; i < input.length; i++) {
      this.buffer[this.writeIndex++] = input[i];

      if (this.writeIndex >= 320) {
        const pcm = new Int16Array(320);
        for (let j = 0; j < 320; j++) {
          const s = Math.max(-1, Math.min(1, this.buffer[j]));
          pcm[j] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage(new Uint8Array(pcm.buffer), [pcm.buffer]);
        this.writeIndex = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm-processor', PcmProcessor);
