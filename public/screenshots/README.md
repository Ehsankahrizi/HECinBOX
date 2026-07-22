# Result figures

Real output from HECinBox runs. All were exported from the Results tab and only
resized, never retouched.

| File | Used on | Shows |
|---|---|---|
| `urban-depth.jpg` | Home, slide 1 | Peak inundation depth across an urban floodplain, 0 to 8 m |
| `urban-velocity.jpg` | Home, slide 2 | Channel velocity along the same reach, cell by cell |
| `valley-depth-3d.jpg` | Home, slide 3 | Depth draped on terrain in the 3D view, a valley reaching 80 m |
| `terrain-3d.jpg` | Home, slide 4 | The 2D computation mesh draped over terrain |
| `mesh-velocity.jpg` | Features | Velocity on a meandering reach with the mesh visible |

## How the homepage slideshow works

The four wide maps cross fade in `#slides`, one every five seconds. Only the
first has a real `src` in the markup; the rest carry `data-src` and are fetched
after the page finishes loading, so first paint is not held up by four maps.

Rotation pauses while the pointer is over the figure and while the browser tab
is in the background. Dots below the image jump straight to a slide. If the
visitor has asked for reduced motion the rotation never starts and the first
map simply stays put.

Images use `object-fit: contain`, never `cover`. Cropping a result map would cut
the colorbar off the edge and make it unreadable, so a slightly different aspect
ratio letterboxes against the neutral background instead.

## Adding or replacing one

Drop the file here and add an `<img>` to the `#slides` block in
`public/index.html`:

```html
<img data-src="/screenshots/your-map.jpg" alt="Short description"
     data-cap="Caption shown under the figure while this slide is up">
```

The dots and the rotation pick it up automatically. Nothing in the JavaScript
needs changing.

## Keep them small

These were 17.6 MB as exported and are 1.3 MB after processing, a 92 percent
saving with no visible loss. Satellite imagery is photographic, so JPEG beats
PNG heavily here. Before adding a new one:

```python
from PIL import Image
im = Image.open("new-map.png").convert("RGB")
w, h = im.size
if w > 1800:
    im = im.resize((1800, round(h * 1800 / w)), Image.LANCZOS)
im.save("new-map.jpg", "JPEG", quality=84, optimize=True, progressive=True)
```

Aim for under 500 KB each.
