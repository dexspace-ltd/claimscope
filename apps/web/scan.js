// Walks a Matterport tour with the Showcase SDK: reads rooms and their sizes, then captures views per room.
import { setupSdk } from "https://cdn.jsdelivr.net/npm/@matterport/sdk@1.6.0/+esm";

const SHOT = { width: 2048, height: 1536 };
// Four walls with some floor in view, then ceiling and floor. Both pitch signs are covered on purpose.
const VIEWS = [
  { yaw: 0, pitch: -15 }, { yaw: 90, pitch: -15 }, { yaw: 180, pitch: -15 }, { yaw: 270, pitch: -15 },
  { yaw: 0, pitch: 55 }, { yaw: 0, pitch: -55 },
];
const LARGE_ROOM_M2 = 12; // rooms bigger than this get a second scan point
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const round2 = (n) => Math.round(n * 100) / 100;

export function modelIdFromUrl(url) {
  const m = /[?&]m=([A-Za-z0-9]{11})(?:&|$)/.exec(url);
  return m ? m[1] : null;
}

export async function connect(container, sdkKey, modelId) {
  return setupSdk(sdkKey, {
    space: modelId,
    container,
    iframeAttributes: { width: "100%", height: "100%", allow: "xr-spatial-tracking" },
    iframeQueryParams: { play: 1, qs: 1, brand: 0, help: 0, hr: 0, mls: 2, title: 0 },
  });
}

// Observable collections fill in over several updates; wait until they stop changing.
function settled(collection, quietMs = 1200, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let latest = {};
    let timer;
    const finish = () => { sub.cancel(); clearTimeout(hardStop); resolve(latest); };
    const sub = collection.subscribe({
      onCollectionUpdated(items) {
        latest = { ...items };
        clearTimeout(timer);
        timer = setTimeout(finish, quietMs);
      },
    });
    const hardStop = setTimeout(finish, timeoutMs);
  });
}

function contains(bounds, p, tol = 0.25) {
  return p.x >= bounds.min.x - tol && p.x <= bounds.max.x + tol
    && p.z >= bounds.min.z - tol && p.z <= bounds.max.z + tol
    && p.y >= bounds.min.y - tol - 1.8 && p.y <= bounds.max.y + tol;
}

const dist = (a, b) => Math.hypot(a.x - b.x, a.z - b.z);

export async function planCapture(sdk) {
  const [rooms, sweeps] = await Promise.all([settled(sdk.Room.data), settled(sdk.Sweep.data)]);
  const sweepList = Object.values(sweeps).filter((s) => s.enabled !== false);
  const roomList = Object.values(rooms);

  const plan = roomList.map((room, i) => {
    const { x, y, z } = room.size;
    const length = round2(Math.max(x, z));
    const breadth = round2(Math.min(x, z));
    const height = y >= 1.8 && y <= 5 ? round2(y) : 2.4;
    const inside = sweepList.filter((s) => contains(room.bounds, s.position));
    inside.sort((a, b) => dist(a.position, room.center) - dist(b.position, room.center));
    const chosen = inside.slice(0, 1);
    if (inside.length > 1 && length * breadth > LARGE_ROOM_M2) {
      const first = chosen[0].position;
      chosen.push(inside.slice(1).reduce((far, s) => (dist(s.position, first) > dist(far.position, first) ? s : far)));
    }
    return {
      scanRoomId: room.id,
      name: room.label?.trim() || `Room ${i + 1}`,
      namePlaceholder: !room.label?.trim(),
      floor: room.floorInfo?.sequence ?? 0,
      dimensions: { length_m: length, breadth_m: breadth, height_m: height, openings: [], source: "scan" },
      sweeps: chosen.map((s) => s.id),
    };
  });
  return { rooms: plan.filter((r) => r.sweeps.length), skipped: plan.filter((r) => !r.sweeps.length), sweepCount: sweepList.length };
}

export const viewsPerSweep = VIEWS.length;

export async function captureSweep(sdk, sweepId, onShot) {
  const instant = sdk.Sweep.Transition?.INSTANT ?? sdk.Camera?.TransitionType?.INSTANT;
  await sdk.Sweep.moveTo(sweepId, { transition: instant, rotation: { x: 0, y: 0 } });
  await sleep(1800); // let the panorama's high-res tiles load
  for (const view of VIEWS) {
    await sdk.Camera.setRotation({ x: view.pitch, y: view.yaw }, { speed: 720 });
    await sleep(900);
    const dataUri = await sdk.Renderer.takeScreenShot(SHOT);
    await onShot(dataUri, { sweep_id: sweepId, ...view });
  }
}
