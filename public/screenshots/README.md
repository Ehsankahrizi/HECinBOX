# Result figures

Drop the four exported maps here using exactly these names. The site picks them
up automatically, no code change needed.

| File name | Which figure |
|---|---|
| `urban-depth.png` | Depth map of the bayou through the dense urban floodplain, colorbar 0 to 8 m |
| `mesh-velocity.png` | Velocity map of the meandering reach with the mesh visible, colorbar 0 to 6.92 m/s |
| `depth-3d.png` | The 3D view with depth draped over terrain, colorbar 0 to 80.47 m |
| `urban-velocity.png` | Velocity along the urban channel, cell by cell, colorbar 0.01 to 1.11 m/s |

## How the gallery behaves

`index.html` requests each file before building the gallery. Any file that is
missing is skipped silently, and if none of them are present the whole Results
section stays hidden. So the page never shows a broken image, and you can add
the figures one at a time.

Images are displayed with `object-fit: contain`, never `cover`, so the colorbar
and the full map extent are always visible rather than cropped away.

## Adding, removing, or re-captioning a figure

Edit the `SHOTS` array in `index.html`, in the results gallery block:

```js
{
  src: "screenshots/your-file.png",
  title: "Short title",
  cap: "One or two sentences explaining what the reader is looking at.",
  wide: true   // spans the full row, for panoramic maps
  // tall: true // 4:3 box instead of 16:9, for nearly square figures
}
```

## Sizing

Export at the resolution you already have. Anything up to roughly 2000 px wide
is fine. If a file is larger than about 1.5 MB, shrink it so the page stays
quick:

```bash
sips -Z 2000 urban-depth.png
```

## Optional: use one as the social preview

The `og:image` tag in `index.html` currently points at `logo.png`. A result map
makes a far more striking link preview. Point it at one of these files instead,
ideally a copy cropped to 1200x630.
