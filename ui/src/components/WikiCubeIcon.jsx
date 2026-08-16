// The wiki brand mark: an isometric knowledge cube in the app's main color.
// Three solid faces at three shades (top brightest, right mid, left dark) so
// the volume reads instantly — even tiny and from a distance — echoing the
// chat agent's single-hue identity without the small-scale noise of a
// sticker/dot grid. Each face is inset toward its own centroid, leaving a
// thin transparent seam between faces (the background shows through) that
// crisps up the silhouette. Pure currentColor; inherits `text-primary`.

const T = [12, 3]
const L = [4, 7.5]
const R = [20, 7.5]
const M = [12, 12]
const BL = [4, 16.5]
const BR = [20, 16.5]
const B = [12, 21]

// Shrink a face toward its centroid: k=1 touches its neighbors, smaller k
// widens the seam between faces (~0.9 ≈ a hairline gap at rendered sizes).
const INSET = 0.9

function facePoints(corners) {
  const cx = corners.reduce((s, c) => s + c[0], 0) / corners.length
  const cy = corners.reduce((s, c) => s + c[1], 0) / corners.length
  return corners
    .map(
      (c) =>
        `${(cx + (c[0] - cx) * INSET).toFixed(2)},${(cy + (c[1] - cy) * INSET).toFixed(2)}`
    )
    .join(" ")
}

export function WikiCubeIcon({ className }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      aria-hidden="true"
    >
      <polygon points={facePoints([T, R, M, L])} fill="currentColor" opacity="1" />
      <polygon points={facePoints([M, R, BR, B])} fill="currentColor" opacity="0.65" />
      <polygon points={facePoints([L, M, B, BL])} fill="currentColor" opacity="0.4" />
    </svg>
  )
}
