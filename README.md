# hecinbox.com

Source for the official HECinBox website. Static, no build step, no
dependencies, no server.

**HECinBox** is an automated 2D HEC-RAS flood simulation pipeline in a
container: it fetches live USGS and NOAA boundary conditions, injects them into
a HEC-RAS model, runs the Linux engine headless, and returns inundation maps,
validation statistics, and threshold alerts.

- Site: https://hecinbox.com
- Application image: https://hub.docker.com/r/ehsankahrizi1991/hecinbox

## This repository holds the website only

The application source and the Docker image are maintained separately. Nothing
here ships inside the published container, and nothing here is required to run
HECinBox. That separation is deliberate: users who pull the image get the
product, not the marketing site.

## Layout

Everything served to visitors lives in `public/`. Everything outside it is
repository material that never reaches the live site.

| Path | Purpose |
|---|---|
| `public/index.html` | The entire site. Inline CSS and JS. |
| `public/404.html` | Not-found page. |
| `public/screenshots/` | Result maps shown in the Results gallery. See its README. |
| `public/logo.png` | Full brand logo, also the social preview image. |
| `public/logo-mark.png` | Logo art without the wordmark, used in the header and footer. |
| `public/favicon.png`, `public/apple-touch-icon.png` | Icons. |
| `public/_headers` | Security headers, applied at the edge. |
| `public/robots.txt`, `public/sitemap.xml` | Search engine files. |
| `wrangler.jsonc` | Cloudflare configuration: publish `public/`, serve `404.html`. |
| `DEPLOY.md` | Hosting setup, DNS, and the pre-launch checklist. |

## Local preview

No tooling required. Open `public/index.html` in a browser, or serve the folder:

```bash
python3 -m http.server 8000 --directory public
```

Then visit http://localhost:8000.

## Deploying

Cloudflare, connected to this repository, so pushing to `main` redeploys the
site. There is no build step and no dependencies to install.

- Build command: leave empty
- Everything else comes from `wrangler.jsonc`

Full setup, custom domain, and the demo HTTPS notes are in [DEPLOY.md](DEPLOY.md).

## Design tokens

The palette and typography are copied from the application theme so the site and
the product read as one piece of software. Values live at the top of the
`<style>` block in `index.html`.

| Token | Value |
|---|---|
| Page background | `#eef0f5` |
| Text | `#0f1e3a` |
| Primary | `#3c78af` |
| Body font | Inter |
| Code font | JetBrains Mono |

## License

MIT for the code, see [LICENSE](LICENSE). The HECinBox logo and brand assets are
not covered by that license and remain the author's property.

HEC-RAS is developed by the US Army Corps of Engineers Hydrologic Engineering
Center. HECinBox is an independent product and is not affiliated with or
endorsed by USACE.
