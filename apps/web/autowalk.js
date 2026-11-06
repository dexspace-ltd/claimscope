// Walks every scan point of a public tour without the SDK. Matterport links accept ss=<scan point>
// and sr=<pitch>,<yaw> (radians), so each view is a fresh load of the embedded tour, captured from
// the shared tab. A scan point number past the end falls back to the tour's start view, which is
// how the walk knows it has finished.
import { diff, fingerprint, tourEmbedUrl } from "./capture.js";

// Three headings 120° apart looking slightly down (walls and floor), then one up at the ceiling.
export const VIEWS = [
  { pitch: -0.3, yaw: 0 }, { pitch: -0.3, yaw: 2.094 }, { pitch: -0.3, yaw: 4.189 }, { pitch: 0.9, yaw: 0 },
];
const MIN_LOAD_MS = 2500;
const MAX_LOAD_MS = 16000;
const SAME_AS_START = 8;  // fingerprint distance under which a view is the fallback start view
const END_AFTER = 3;      // consecutive fallbacks before a lane stops
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function variance(print) {
  let mean = 0;
  for (const v of print) mean += v;
  mean /= print.length;
  let total = 0;
  for (const v of print) total += (v - mean) ** 2;
  return total / print.length;
}

// Resolves once the frame has a real picture (not a blank loading screen) that has stopped changing.
async function settle(capture, frame, signal) {
  const started = Date.now();
  await sleep(MIN_LOAD_MS);
  let previous = null;
  let steady = 0;
  while (Date.now() - started < MAX_LOAD_MS) {
    if (signal.aborted) throw new DOMException("Stopped", "AbortError");
    const print = fingerprint(capture.grab(64, frame));
    if (previous && diff(print, previous) < 3 && variance(print) > 150) {
      if (++steady >= 2) return print;
    } else {
      steady = 0;
    }
    previous = print;
    await sleep(450);
  }
  return previous;
}

async function show(frame, url, capture, signal) {
  frame.src = url;
  return settle(capture, frame, signal);
}

export async function autoWalk({ host, modelId, capture, lanes = 1, maxScanPoints = 150, onView, onProgress, signal }) {
  const frames = Array.from({ length: lanes }, () => {
    const f = document.createElement("iframe");
    f.allow = "xr-spatial-tracking";
    f.title = "Matterport tour (auto-walk)";
    host.append(f);
    return f;
  });

  onProgress({ phase: "Loading the tour's start view…", scanPoint: 0, views: 0 });
  const startPrint = await show(frames[0], tourEmbedUrl(modelId, `&ss=${maxScanPoints + 500}&sr=${VIEWS[0].pitch},${VIEWS[0].yaw}`), capture, signal);

  let views = 0;
  let furthest = 0;
  let finished = 0;

  async function lane(frame, first) {
    let fallbacks = 0;
    for (let ss = first; ss <= maxScanPoints; ss += lanes) {
      for (const [i, view] of VIEWS.entries()) {
        const print = await show(frame, tourEmbedUrl(modelId, `&ss=${ss}&sr=${view.pitch},${view.yaw}`), capture, signal);
        if (i === 0) {
          if (print && startPrint && diff(print, startPrint) < SAME_AS_START) {
            if (++fallbacks >= END_AFTER) return;
            break; // looks like the start view: skip this scan point's other views
          }
          fallbacks = 0;
        }
        const canvas = capture.grab(2048, frame);
        const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.9));
        await onView(blob, { sweep_id: `ss${ss}`, yaw: Math.round(view.yaw * 57.3), pitch: Math.round(view.pitch * 57.3) });
        views++;
        furthest = Math.max(furthest, ss);
        onProgress({ phase: `Scan point ${ss}`, scanPoint: furthest, views, viewInPoint: i + 1, viewsPerPoint: VIEWS.length });
      }
    }
  }

  try {
    await Promise.all(frames.map((frame, i) => lane(frame, i + 1).finally(() => finished++)));
  } finally {
    frames.forEach((f) => f.remove());
  }
  return { views, scanPoints: furthest };
}
