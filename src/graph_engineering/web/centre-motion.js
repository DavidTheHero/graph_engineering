export function quadraticPoint(start, control, end, progress) {
  const t = clampProgress(progress);
  const s = point(start);
  const c = point(control);
  const e = point(end);
  const u = 1 - t;
  return {
    x: u * u * s.x + 2 * u * t * c.x + t * t * e.x,
    y: u * u * s.y + 2 * u * t * c.y + t * t * e.y
  };
}

function clampProgress(progress) {
  if (typeof progress !== 'number' || !Number.isFinite(progress)) {
    throw new TypeError('progress must be a finite number');
  }
  return Math.min(1, Math.max(0, progress));
}

function point(p) {
  if (typeof p !== 'object' || p === null) {
    throw new TypeError('point must be an object with numeric x and y');
  }
  if (typeof p.x !== 'number' || !Number.isFinite(p.x)) {
    throw new TypeError('point.x must be a finite number');
  }
  if (typeof p.y !== 'number' || !Number.isFinite(p.y)) {
    throw new TypeError('point.y must be a finite number');
  }
  return p;
}
