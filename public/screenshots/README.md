# Screenshots

## The one file the site needs

Save one of your result maps here as **`hero.png`**. It becomes the large image
in the centre of the homepage.

The wide urban depth map is the best choice: it reads instantly as a flood map,
and its shape suits a full width slot.

Until that file exists the homepage draws a generated illustration in its place,
so the page never looks broken. The generated one is obviously synthetic
though. A real result is far more convincing.

## Sizing

Export at the width you already have, up to about 2000 px. If the file is
larger than roughly 1 MB, shrink it so the homepage stays quick:

```bash
sips -Z 1800 hero.png
```

## Using more of them later

Extra maps can go on the Features page. Drop the file here, then add an `<img>`
to `public/features/index.html`. Nothing else needs changing.

## Optional: use one as the social preview

`og:image` currently points at `logo.png`, which is square and crops awkwardly
in Slack and LinkedIn. A result map cropped to 1200x630 makes a much stronger
link preview. Point the tag in each page's `<head>` at it once you have one.
