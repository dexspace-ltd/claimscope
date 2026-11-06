// Captures views of an embedded public Matterport tour without the SDK, using the browser's
// tab-sharing permission. The assessor walks the tour; a view is kept whenever the camera
// settles on something new.

const MAX_EDGE = 2048;
const SAMPLE_MS = 600;
const STILL_DIFF = 6;   // mean pixel change (0-255) under which the camera counts as settled
const NEW_VIEW_DIFF = 22; // change from the last kept view needed to keep another

const CLEAN_UI = "play=1&qs=1&brand=0&help=0&hr=0&title=0&hl=0&mt=0&pin=0&portal=0&search=0&tourcta=0&vr=0&f=0&gt=0&wh=0";

export function tourEmbedUrl(modelId, extra = "") {
  return `https://my.matterport.com/show/?m=${modelId}&${CLEAN_UI}${extra}`;
}

export class TabCapture {
  constructor(frameEl) {
    this.frameEl = frameEl; // the element whose on-screen area is cropped out of the tab image
    this.stream = null;
    this.video = document.createElement("video");
    this.video.muted = true;
    this.video.playsInline = true;
    this.auto = null;
  }

  get active() {
    return Boolean(this.stream?.active);
  }

  async start() {
    if (!navigator.mediaDevices?.getDisplayMedia) {
      throw new Error("This browser can't share a tab. Use a recent Chrome or Edge.");
    }
    this.stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "browser", frameRate: 5 },
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: "include",
    });
    this.video.srcObject = this.stream;
    await this.video.play();
    this.stream.getVideoTracks()[0].addEventListener("ended", () => this.stopAuto());
  }

  stop() {
    this.stopAuto();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  // Crop an element's on-screen rectangle (the tour by default) out of the tab frame.
  grab(maxEdge = MAX_EDGE, element = this.frameEl) {
    const v = this.video;
    if (!v.videoWidth) throw new Error("Tab capture has no picture yet.");
    const rect = element.getBoundingClientRect();
    const sx = v.videoWidth / window.innerWidth;
    const sy = v.videoHeight / window.innerHeight;
    const crop = {
      x: Math.max(0, rect.left * sx), y: Math.max(0, rect.top * sy),
      w: Math.min(v.videoWidth, rect.width * sx), h: Math.min(v.videoHeight, rect.height * sy),
    };
    const scale = Math.min(1, maxEdge / Math.max(crop.w, crop.h));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(crop.w * scale);
    canvas.height = Math.round(crop.h * scale);
    canvas.getContext("2d").drawImage(v, crop.x, crop.y, crop.w, crop.h, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  async snapshot() {
    const rect = this.frameEl.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) throw new Error("Scroll the tour into view first.");
    const canvas = this.grab();
    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
  }

  // Keep a view each time the camera settles somewhere new. onView receives a JPEG blob.
  startAuto(onView) {
    this.stopAuto();
    let previous = null;
    let lastKept = null;
    let busy = false;
    this.auto = setInterval(async () => {
      if (busy || !this.active) return;
      const thumb = fingerprint(this.grab(64));
      const still = previous && diff(thumb, previous) < STILL_DIFF;
      previous = thumb;
      if (!still || (lastKept && diff(thumb, lastKept) < NEW_VIEW_DIFF)) return;
      busy = true;
      try {
        lastKept = thumb;
        await onView(await this.snapshot());
      } finally {
        busy = false;
      }
    }, SAMPLE_MS);
  }

  stopAuto() {
    clearInterval(this.auto);
    this.auto = null;
  }
}

export function fingerprint(canvas) {
  const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
  const grey = new Uint8Array(data.length / 4);
  for (let i = 0; i < grey.length; i++) grey[i] = (data[i * 4] * 3 + data[i * 4 + 1] * 6 + data[i * 4 + 2]) / 10;
  return grey;
}

export function diff(a, b) {
  if (a.length !== b.length) return 255;
  let total = 0;
  for (let i = 0; i < a.length; i++) total += Math.abs(a[i] - b[i]);
  return total / a.length;
}
